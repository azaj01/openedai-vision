import io
import os
import time
import requests
import tempfile
import queue
from threading import Thread
from datauri import DataURI
from PIL import Image
import torch
from typing import Optional, List, Literal, AsyncGenerator, Union, Any
from pydantic import BaseModel
from transformers import BitsAndBytesConfig, TextIteratorStreamer, AutoConfig
from loguru import logger
from mistral_common.protocol.instruct.messages import UserMessage, TextChunk, ImageURLChunk, SystemMessage, AssistantMessage, ToolMessage
from mistral_common.protocol.instruct.request import ChatCompletionRequest

# When models require an image but no image given
black_pixel_url = 'data:image/png;charset=utf-8;base64,iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAADElEQVQI12NgGB4AAADIAAF8Y2l9AAAAAElFTkSuQmCC'
transparent_pixel_url = 'data:image/png;charset=utf-8;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVQI12P4//8/AAX+Av7czFnnAAAAAElFTkSuQmCC'

class ImageURL(BaseModel):
    url: str
    detail: Optional[str] = "auto" # auto -> low (512) or high (Nx512) based on res.

class Content(BaseModel):
    type: Literal["text", "image_url"]
    text: Optional[str] = None
    image_url: Optional[ImageURL] = None

class Message(BaseModel):
    role: str
    content: Union[str, List[Content]]
    name: str = None

class ImageChatRequest(BaseModel):
    messages: List[Message]
    model: str # = "gpt-4-vision-preview"
    frequency_penalty: float = 0.0
    logit_bias: dict = None
    logprobs: bool = False
    top_logprobs: int = None
    max_tokens: int = 512 # Deprecated
    max_completion_tokens: int = 1024
    n: int = 1
    presence_penalty: float = 0.0
    response_format: str = None
    seed: int = None
    service_tier: str = None
    stop: Union[str,List[str]] = None
    stream: bool = False
    stream_options: dict = None
    temperature: float = None # 1.0
    top_p: float = None # 1.0
    tools: List[dict] = None
    tool_choice: Union[str,dict] = None
    parallel_tool_calls: bool = True
    user: str = None
    function_call: Union[str,dict] = None # deprecated
    functions: List[dict] = None # deprecated

class VisionQnABase:
    model_name: str = None
    format: str = None
    revision: str = 'main'
    vision_layers: List[str] = [] # "vision_model", "resampler", "vision", "vision_tower"]

    def __init__(self, model_id: str, device: str, device_map: str = 'auto', extra_params = {}, format = None):
        self._model_id = model_id

        self.device, self.dtype = self.select_device_dtype(device)

        self.params = {
            'pretrained_model_name_or_path': model_id,
            'torch_dtype': self.dtype,
            'low_cpu_mem_usage': True,
            'revision': self.revision,
            'device_map': device_map,
            'trust_remote_code': extra_params.get('trust_remote_code', False)
        }

        if extra_params.get('load_in_4bit', False):
            load_in_4bit_params = {
                'quantization_config': BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_quant_type='nf4',
                    bnb_4bit_compute_dtype=self.dtype,
                    llm_int8_skip_modules=self.vision_layers,
                )
            }
            if extra_params.get('4bit_use_double_quant', False):
                load_in_4bit_params['quantization_config'].bnb_4bit_use_double_quant = True

            self.params.update(load_in_4bit_params)
        elif extra_params.get('load_in_8bit', False):
            load_in_8bit_params = {
                'quantization_config': BitsAndBytesConfig(
                    load_in_8bit=True,
                    llm_int8_skip_modules=self.vision_layers,
                )
            }
            self.params.update(load_in_8bit_params)

        if format:
            self.format = format

        torch.set_grad_enabled(False)

    def loaded_banner(self):
        logger.info(f"Loaded {self._model_id} [ device: {self.model.device}, dtype: {self.model.dtype}, template: {self.format} ]")

    def select_device(self):
        return 'cuda' if torch.cuda.is_available() else 'mps' if torch.backends.mps.is_available() else 'cpu'

    def select_dtype(self, device):
        return torch.float32 if device == 'cpu' else torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16

    def select_device_dtype(self, device):
        device = self.select_device() if device == 'auto' else device
        dtype = self.select_dtype(device)
        return device, dtype

    def repack_message_content(self, request: ImageChatRequest) -> ImageChatRequest:
        """ Repack messages to remove string "content" messages and convert to List[Content] """

        for m in request.messages:
            if isinstance(m.content, str):
                m.content = [ Content(type='text', text=m.content) ]

        return request

    # implement one or both of the stream/chat_with_images functions
    async def chat_with_images(self, request: ImageChatRequest) -> str:
        tps_start = time.time()
        resp = [r async for r in self.stream_chat_with_images(request)]
        logger.info(f"Generated {len(resp)} tokens at {len(resp) / (time.time() - tps_start):0.2f} T/s")
        return ''.join(resp)

    # implement one or both of the stream/chat_with_images functions
    async def stream_chat_with_images(self, request: ImageChatRequest) -> AsyncGenerator[str, None]:
        yield await self.chat_with_images(request)

    def get_generation_params(self, request: ImageChatRequest, default_params = {}) -> dict:
        params = {
            'top_k': None,
            'do_sample': False,
            'use_cache': True,
        }
        params.update(default_params)

        if request.max_tokens:
            params["max_new_tokens"] = request.max_tokens

        if request.temperature is not None:
            if request.temperature > 0:
                params["do_sample"] = True
                params["temperature"] = request.temperature

        if request.top_p is not None and request.top_p != params.get('top_p', 1.0):
            params["do_sample"] = True
            params["top_p"] = request.top_p

        return params

def threaded_streaming_generator(generate, tokenizer, generation_kwargs):
    streamer = TextIteratorStreamer(tokenizer, skip_special_tokens=True, skip_prompt=True, timeout=600)

    generation_kwargs['streamer'] = streamer

    exq = queue.Queue()

    def wrapper():
        try:
            with torch.no_grad():
                generate(**generation_kwargs)

        except Exception as e:
            #logger.exception(e)
            exq.put(e)
            streamer.end()

    t = Thread(target=wrapper, daemon=True)
    t.start()

    for text in streamer:
        if text:
            yield text

    if not exq.empty():
        raise exq.get_nowait()

def join_int_lists(int_lists, separator):
    result = []
    for i, lst in enumerate(int_lists):
        result.extend(lst)
        if i < len(int_lists) - 1:
            result.extend([separator])
    return result

async def url_to_image(img_url: str) -> Image.Image:
    if img_url.startswith('http'):
        response = requests.get(img_url)

        img_data = response.content
    elif img_url.startswith('data:'):
        img_data = DataURI(img_url).data

    return Image.open(io.BytesIO(img_data)).convert("RGB")

async def url_to_file(img_url: str) -> str:
    mime_map = {
        'image/png': '.png',
        'image/x-png': '.png',
        'image/jpg': '.jpg',
        'image/jpeg': '.jpeg',
        'image/gif': '.gif',
        'image/webp': '.webp',
        'video/avi': '.avi',
        'video/mp4': '.mp4',
        'video/mpeg': '.mpeg',
        'video/mov': '.mov',
        'video/mkv': '.mkv',
        'video/wmv': '.wmv',
        'video/webm': '.webm',
    }
    if img_url.startswith('data:'):
        dui = DataURI(img_url)
        ext = mime_map.get(dui.mimetype, '.mp4' if 'video/' in dui.mimetype else '.png')
        of, filename = tempfile.mkstemp(suffix=ext)
        os.write(of, dui.data)
        return filename
    else:
        response = requests.get(img_url)
        mime_type = response.headers.get('Content-Type', 'image/png')
        ext = mime_map.get(mime_type, '.mp4' if 'video/' in mime_type else '.png')
        fd, filename = tempfile.mkstemp(suffix=ext)
        os.write(fd, response.content)
        return filename

async def images_hfmessages_from_messages(messages: list[Message], url_handler = url_to_image):
    hfmessages = []
    images = []

    for m in messages:
        content = []
        for c in m.content:
            if c.type == 'image_url':
                image = await url_handler(c.image_url.url)
                images.extend([image])
                content.extend([{"type": "image"}])
            elif c.type == 'text':
                content.extend([{'type': 'text', 'text': c.text}])

        hfmessages.extend([{'role': m.role, 'content': content}])

    return images, hfmessages

async def messages_from_messages(messages: list[Message]): 
    conversation = []

    for m in messages:
        content = []
        for c in m.content:
            if c.type == 'image_url':
                content.extend([{"type": "image", "url": c.image_url.url}]) # image url remains unchanged
            elif c.type == 'text':
                content.extend([{'type': 'text', 'text': c.text}])

        conversation.extend([{'role': m.role, 'content': content}])

    return conversation


async def phi15_prompt_from_messages(messages: list[Message], img_tok = "<image>", img_end = '', url_handler = url_to_image): # </image>
    prompt = ''
    images = []
    generation_msg = "Answer:"

    if messages and messages[-1].role == 'assistant':
        generation_msg += messages[-1].content[0].text
        messages.pop(-1)

    for m in messages:
        if m.role == 'user':
            p = ''
            for c in m.content:
                if c.type == 'image_url':
                    img_data = await url_handler(c.image_url.url)
                    images.extend([img_data])
                    p = img_tok.format(img_data) + p + img_end # this is a bit strange, but it works for Monkey and filenames
                if c.type == 'text':
                    p += f"{c.text}\n\n" # Question:
            prompt += p
        elif m.role == 'assistant':
            for c in m.content:
                if c.type == 'text':
                    prompt += f"Answer: {c.text}\n\n"
        elif m.role == 'system':
            for c in m.content:
                if c.type == 'text':
                    prompt += f"{c.text}\n\n"  # fake system prompt

    prompt += generation_msg

    return images, prompt

async def vicuna0_prompt_from_messages(messages: list[Message], img_tok = "<image_placeholder>\n"):
    prompt = ''
    images = []
    generation_msg = "### Assistant:"

    if messages and messages[-1].role == 'assistant':
        generation_msg += messages[-1].content[0].text
        messages.pop(-1)

    for m in messages:
        if m.role == 'user':
            text = ''
            img_tag = ''

            for c in m.content:
                if c.type == 'image_url':
                    images.extend([ await url_to_image(c.image_url.url) ])
                    img_tag += img_tok
                if c.type == 'text':
                    text = c.text

            prompt += f"### Human: {img_tag}{text}\n"
        elif m.role == 'assistant':
            for c in m.content:
                if c.type == 'text':
                    prompt += f"### Assistant: {c.text}\n"
        elif m.role == 'system':
            for c in m.content:
                if c.type == 'text':
                    prompt += f"{c.text}\n\n"

    prompt += generation_msg

    return images, prompt


async def vicuna_prompt_from_messages(messages: list[Message], img_tok = "<image>\n"):
    prompt = ''
    images = []
    generation_msg = "ASSISTANT:"

    if messages and messages[-1].role == 'assistant':
        generation_msg += messages[-1].content[0].text
        messages.pop(-1)

    for m in messages:
        if m.role == 'user':
            text = ''
            img_tag = ''

            for c in m.content:
                if c.type == 'image_url':
                    images.extend([ await url_to_image(c.image_url.url) ])
                    img_tag += img_tok
                if c.type == 'text':
                    text = c.text

            prompt += f"USER: {img_tag}{text}\n"
        elif m.role == 'assistant':
            for c in m.content:
                if c.type == 'text':
                    prompt += f"ASSISTANT: {c.text}\n"
        elif m.role == 'system':
            for c in m.content:
                if c.type == 'text':
                    prompt += f"{c.text}\n\n"

    prompt += generation_msg

    return images, prompt

async def llama2_prompt_from_messages(messages: list[Message], img_tok = "<image>\n"):
    prompt = ''
    images = []

    for m in messages:
        if m.role == 'user':
            text = ''
            img_tag = ''

            for c in m.content:
                if c.type == 'image_url':
                    images.extend([ await url_to_image(c.image_url.url) ])
                    img_tag += img_tok
                if c.type == 'text':
                    text = c.text

            prompt += f"[INST] {img_tag}{text} [/INST]"
        elif m.role == 'assistant':
            for c in m.content:
                if c.type == 'text':
                    prompt += f" {c.text}"
        elif m.role == 'system':
            for c in m.content:
                if c.type == 'text':
                    prompt += f"[INST] <<SYS>>\n{c.text}\n<</SYS>> [/INST]" # not quite right, but it's a start

    return images, prompt

async def llama3_prompt_from_messages(messages: list[Message], img_tok = "<image>"):
    prompt = ''
    images = []
    generation_msg = '<|start_header_id|>assistant<|end_header_id|>\n\n'

    if messages and messages[-1].role == 'assistant':
        generation_msg += messages[-1].content[0].text
        messages.pop(-1)

    for m in messages:
        img_tag = ''

        for c in m.content:
            if c.type == 'image_url':
                images.extend([ await url_to_image(c.image_url.url) ])
                img_tag += img_tok

        for c in m.content:
            if c.type == 'text':
                prompt += f"<|start_header_id|>{m.role}<|end_header_id|>\n\n{img_tag}{c.text.strip()}<|eot_id|>"

    prompt += generation_msg

    return images, prompt

async def chatml_prompt_from_messages(messages: list[Message], img_tok = "<image>\n"):
    prompt = ''
    images = []
    generation_msg = "<|im_start|>assistant\n"

    if messages and messages[-1].role == 'assistant':
        generation_msg += messages[-1].content[0].text
        messages.pop(-1)

    for m in messages:
        if m.role == 'user':
            text = ''
            img_tag = ''

            for c in m.content:
                if c.type == 'image_url':
                    images.extend([ await url_to_image(c.image_url.url) ])
                    img_tag += img_tok
                if c.type == 'text':
                    text = c.text

            prompt += f"<|im_start|>user\n{img_tag}{text}<|im_end|>"
        elif m.role == 'assistant':
            for c in m.content:
                if c.type == 'text':
                    prompt += f"<|im_start|>assistant\n{c.text}<|im_end|>"
        elif m.role == 'system':
            for c in m.content:
                if c.type == 'text':
                    prompt += f"<|im_start|>system\n{c.text}<|im_end|>"

    prompt += generation_msg

    return images, prompt

async def gemma_prompt_from_messages(messages: list[Message], img_tok = "<image>\n"):
    prompt = ''
    images = []
    generation_msg = "<start_of_turn>model\n"

    if messages and messages[-1].role == 'assistant':
        generation_msg += messages[-1].content[0].text
        messages.pop(-1)

    for m in messages:
        if m.role == 'user':
            text = ''
            img_tag = ''

            for c in m.content:
                if c.type == 'image_url':
                    images.extend([ await url_to_image(c.image_url.url) ])
                    img_tag += img_tok
                if c.type == 'text':
                    text = c.text

            prompt += f"<start_of_turn>user\n{img_tag}{text}<end_of_turn>"
        elif m.role == 'assistant':
            for c in m.content:
                if c.type == 'text':
                    prompt += f"<start_of_turn>model\n{c.text}<end_of_turn>"
        elif m.role == 'system':
            for c in m.content:
                if c.type == 'text':
                    prompt += f"<start_of_turn>system\n{c.text}<end_of_turn>" # fake it


    prompt += generation_msg

    return images, prompt

async def fuyu_prompt_from_messages(messages: list[Message], img_tok = "", img_end = ''):
    prompt = ''
    images = []

    for m in messages:
        if m.role == 'user':
            p = ''
            for c in m.content:
                if c.type == 'image_url':
                    images.extend([ await url_to_image(c.image_url.url) ])
                    p = img_tok + p + img_end # XXX
                if c.type == 'text':
                    p += f"{c.text}\n\n" # Question:
            prompt += p
        elif m.role == 'assistant':
            for c in m.content:
                if c.type == 'text':
                    prompt += f"\x04{c.text}\n"
        elif m.role == 'system':
            for c in m.content:
                if c.type == 'text':
                    prompt += f"{c.text}\n\n" # fake system prompt doesn't work.

    return images, prompt

async def emu_images_prompt_system_from_messages(messages: list[Message], img_tok = "[<IMG_PLH>]"):
    prompt = ''
    images = []
    system_message = None

    generation_msg = ' [ASSISTANT]:'

    if messages and messages[-1].role == 'assistant':
        generation_msg += messages[-1].content[0].text
        messages.pop(-1)

    for m in messages:
        if m.role == 'user':
            text = ''
            img_tag = ''

            for c in m.content:
                if c.type == 'image_url':
                    images.extend([ await url_to_image(c.image_url.url) ])
                    img_tag += img_tok
                if c.type == 'text':
                    text = c.text

            prompt += f" [USER]: {img_tag}{text}"
        elif m.role == 'assistant':
            for c in m.content:
                if c.type == 'text':
                    prompt += f" [ASSISTANT]: {c.text}</s>"
        elif m.role == 'system':
            for c in m.content:
                if c.type == 'text':
                    system_message = c.text

    prompt += generation_msg

    return images, prompt, system_message

# img_tok = "<|image_{}|>\n" is also ok
async def phi3_prompt_from_messages(messages: list[Message], img_tok = "<image>\n"):
    n = 1
    prompt = ''
    images = []
    generation_msg = '<|assistant|>\n'

    if messages and messages[-1].role == 'assistant':
        generation_msg += messages[-1].content[0].text
        messages.pop(-1)

    for m in messages:
        img_tag = ''

        for c in m.content:
            if c.type == 'image_url':
                images.extend([ await url_to_image(c.image_url.url) ])
                img_tag += img_tok.format(n)
                n += 1

        for c in m.content:
            if c.type == 'text':
                prompt += f"<|{m.role}|>\n{img_tag}{c.text}<|end|>\n"

    prompt += generation_msg

    return images, prompt

async def phintern_prompt_from_messages(messages: list[Message], img_tok = "<image>\n"):
    prompt = ''
    images = []
    generation_msg = "<s><|assistant|>\n"

    if messages and messages[-1].role == 'assistant':
        generation_msg += messages[-1].content[0].text
        messages.pop(-1)

    for m in messages:
        if m.role == 'user':
            text = ''
            img_tag = ''

            for c in m.content:
                if c.type == 'image_url':
                    images.extend([ await url_to_image(c.image_url.url) ])
                    img_tag += img_tok
                if c.type == 'text':
                    text = c.text

            prompt += f"<s><|user|>\n{img_tag}{text}<|end|>"
        elif m.role == 'assistant':
            for c in m.content:
                if c.type == 'text':
                    prompt += f"<s><|assistant|>\n{c.text}<|end|>"

    prompt += generation_msg

    return images, prompt

async def falcon_prompt_from_messages(messages: list[Message], img_tok = "<image>\n"):
    prompt = ''
    images = []
    generation_msg = "Falcon:"

    if messages and messages[-1].role == 'assistant':
        generation_msg += messages[-1].content[0].text
        messages.pop(-1)

    for m in messages:
        if m.role == 'user':
            text = ''
            img_tag = ''

            for c in m.content:
                if c.type == 'image_url':
                    images.extend([ await url_to_image(c.image_url.url) ])
                    img_tag += img_tok
                if c.type == 'text':
                    text = c.text

            prompt += f"User:{img_tag}{text} "
        elif m.role == 'assistant':
            for c in m.content:
                if c.type == 'text':
                    prompt += f"Falcon:{c.text}"
        elif m.role == 'system':
            for c in m.content:
                if c.type == 'text':
                    prompt += f"{c.text}\n\n"

    prompt += generation_msg

    return images, prompt

async def prompt_history_images_system_from_messages(messages: list[Message], img_tok = "<image>\n", url_handler = url_to_image):
    history = []
    images = []
    prompt = ''
    system_prompt = None

    for m in messages:
        if m.role == 'user':
            p = ''
            for c in m.content:
                if c.type == 'image_url':
                    image = await url_handler(c.image_url.url)
                    images.extend([image])
                    p = img_tok + p # XXX Wrong order?
                if c.type == 'text':
                    p += c.text

            prompt += p
        elif m.role == 'assistant':
            for c in m.content:
                if c.type == 'text':
                    history.extend([(prompt, c.text)])
                    prompt = ''
        elif m.role == 'system':
            for c in m.content:
                if c.type == 'text':
                    system_prompt = c.text

    return prompt, history, images, system_prompt

async def glm4v_prompt_from_messages(messages: list[Message], img_tok = "<|begin_of_image|><|endoftext|><|end_of_image|>", url_handler = url_to_image):
    prompt = '[gMASK]<sop>'
    images = []
    generation_msg = '<|assistant|>\n'

    if messages and messages[-1].role == 'assistant':
        generation_msg += messages[-1].content[0].text
        messages.pop(-1)

    for m in messages:
        img_tag = ''
        metadata = '' # not used

        # TODO: handle tool role and build system prompt?

        for c in m.content:
            if c.type == 'image_url':
                images.extend([ await url_handler(c.image_url.url) ])
                img_tag += img_tok

        for c in m.content:
            if c.type == 'text':
                prompt += f"<|{m.role}|>{metadata}\n{img_tag}{c.text}"

    prompt += generation_msg

    return images, prompt

async def florence_prompt_from_messages(messages: list[Message], url_handler = url_to_image):
    prompt = '<MORE_DETAILED_CAPTION>' # "<CAPTION>", "<DETAILED_CAPTION>", "<MORE_DETAILED_CAPTION>", "<OCR>"
    images = []

    for m in messages:
        for c in m.content:
            if c.type == 'image_url':
                images.extend([ await url_handler(c.image_url.url) ])

        for c in m.content:
            if c.type == 'text' and c.text:
                prompt = c.text # only one command at a time

    return images, prompt

async def pixtral_prompt_from_messages(messages: list[Message], img_tok = "[IMG]", url_handler = url_to_image):
    prompt = '<s>'
    images = []

    system_prompt = None
    generation_msg = ''
    last_message = ''

    if messages and messages[-1].role == 'assistant':
        generation_msg += messages[-1].content[0].text
        messages.pop(-1)

    if messages and messages[-1].role == 'user':
        last_message = messages[-1].content[0].text
        messages.pop(-1)

    for m in messages:
        if m.role == 'user':
            text = ''
            img_tag = ''

            for c in m.content:
                if c.type == 'image_url':
                    images.extend([ await url_to_image(c.image_url.url) ])
                    img_tag += img_tok
                if c.type == 'text':
                    text = c.text

            prompt += f"[INST] {text}{img_tag} [/INST]"
        elif m.role == 'assistant':
            for c in m.content:
                if c.type == 'text':
                    prompt += f" {c.text}"
        elif m.role == 'system':
            for c in m.content:
                if c.type == 'text':
                    system_prompt += c.text
#        elif m.role == 'tool':
#            ...

    if system_prompt:
        last_message = system_prompt + '\n\n' + last_message

    last_message = "[INST] " + last_message + " [/INST]"
    if generation_msg:
        last_message += generation_msg

    prompt += generation_msg

    return images, prompt

async def pixtral_messages(messages: list[Message]):
    pix_messages = []

 #   generation_msg = ''

#    if messages and messages[-1].role == 'assistant':
#        generation_msg += messages[-1].content[0].text
#        messages.pop(-1)

    for m in messages:
        content = []
        text = ''
        for c in m.content:
            if c.type == 'text' and c.text:
                text = c.text
                content.extend([TextChunk(text=c.text)])
            if c.type == 'image_url':
                content.extend([ ImageURLChunk(image_url=c.image_url.url) ])

        if m.role == 'user':
            pix_messages.extend([UserMessage(content=content)])
        elif m.role == 'assistant':
            pix_messages.extend([AssistantMessage(content=text)])
        elif m.role == 'system':
            pix_messages.extend([SystemMessage(content=text)])
#        elif m.role == 'tool':
#            pix_messages.extend([ToolMessage(content=text, tool_call_id=])

    return ChatCompletionRequest(messages=pix_messages, model="pixtral")

async def prompt_from_messages(messages: list[Message], format: str) -> str:
    known_formats = {
        'chatml': chatml_prompt_from_messages,
        'falcon': falcon_prompt_from_messages,
        'florence': florence_prompt_from_messages,
        'fuyu': fuyu_prompt_from_messages,
        'gemma': gemma_prompt_from_messages,
        'glm4v': glm4v_prompt_from_messages,
        'llama2': llama2_prompt_from_messages,
        'llama3': llama3_prompt_from_messages,
        'mistral': llama2_prompt_from_messages, # simplicity
        'phi15': phi15_prompt_from_messages,
        'phi3': phi3_prompt_from_messages,
        'phintern': phintern_prompt_from_messages,
        'pixtral': pixtral_prompt_from_messages,
        'vicuna': vicuna_prompt_from_messages,
        'vicuna0': vicuna0_prompt_from_messages,
    }

    if format not in known_formats:
        raise ValueError(f"Unknown format: {format}")

    return await known_formats[format](messages)

def guess_model_format(model_name: str) -> str:
    model_id = model_name.lower()

    model_format_match_map = {
        'chatml': ['34b', 'yi-6b', 'nanollava', 'internvl-chat-v1-5', 'internvl-chat-2b', 'internvl2-', 'internvl2_5-', 'llava-onevision', 'aquila'],
        'falcon': ['falcon'],
        'florence': ['florence'],
        'fuyu': ['fuyu'],
        'gemma': ['gemma'],
        'glm4v': ['glm-4v'],
        'llama2': ['bakllava', '8x7b', 'mistral', 'mixtral'],
        'llama3': ['llama-3-vision', '360vl', 'llama3'],
        'phi15': ['moondream1', 'moondream2', 'monkey'],
        'phi3': ['phi3', 'phi-3'],
        'phintern': ['internvl-chat-4b', 'opengvlab/internvl2-4b'],
        'pixtral': ['pixtral'],
        'vicuna': ['vicuna', '13b'],
        'vicuna0': ['yi-vl'],
    }
    # Exact match first
    for format, options in model_format_match_map.items():
        if model_id in options:
            return format
    for format, options in model_format_match_map.items():
        if any(x in model_id for x in options):
            return format


    return 'vicuna'


def guess_backend(model_name: str, trust_remote_code = True) -> str:

    model_id = model_name.lower()

    if 'aria' in model_id:
        return 'aria' # idefics3_vision
    if 'ovis2' in model_id:
        return 'ovis2'
    if 'ovis1.6' in model_id:
        return 'ovis16'
    if 'aquila' in model_id:
        return 'llavanextgit'
    if 'bunny' in model_id:
        return 'bunny'
    if 'cogvlm2' in model_id:
        return 'cogvlm2'
    if 'cogvlm' in model_id or 'cogagent' in model_id:
        return 'cogvlm'
    if 'dolphin' in model_id:
        return 'dv-qwen'
    if 'emu2' in model_id:
        return 'emu'
    if 'joy-caption-alpha-two' in model_id:
        return 'joy-caption-latest'
    if 'joy-caption-pre-alpha' in model_id:
        return 'joy-caption-pre-alpha'
    if 'llava-onevision' in model_id:
        return 'llavanextgit'
    if 'mantis' in model_id:
        return 'mantis'
    if 'minimonkey' in model_id: # internvl_chat?
        return 'minimonkey'
    if 'minicpm-v-2_6' in model_id:
        return 'minicpm-v-2_6'
    if 'moondream2' in model_id:
        return 'moondream2'

    try:
        conf = AutoConfig.from_pretrained(model_id, trust_remote_code=trust_remote_code)
        return conf.model_type.lower()

    except Exception as e:
        print(f"Error, unable to guess backend or unsupported backend. To specify backend use -b/--backend")
        raise e
