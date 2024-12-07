FROM python:3.11-slim

RUN apt-get update && apt-get install -y git gcc \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/pip pip install --upgrade pip

WORKDIR /app
RUN git clone https://github.com/TIGER-AI-Lab/Mantis.git --single-branch /app/Mantis && \
    git clone https://github.com/baaivision/Emu3 --single-branch /app/Emu3

COPY requirements.txt .
ARG VERSION=latest
RUN if [ "$VERSION" = "alt" ]; then echo "transformers==4.41.2" >> requirements.txt; else echo "transformers>=4.47.0" >> requirements.txt ; fi
RUN --mount=type=cache,target=/root/.cache/pip pip install -U -r requirements.txt

WORKDIR /app/Mantis
RUN --mount=type=cache,target=/root/.cache/pip pip install --no-deps -e .

WORKDIR /app

COPY *.py model_conf_tests.json README.md LICENSE /app/
COPY backend /app/backend

ARG USER_ID=1000
ENV USER_ID=${USER_ID}
ARG GROUP_ID=1000
ENV GROUP_ID=${GROUP_ID}
RUN groupadd -g ${GROUP_ID} openedai && \
    useradd -r -u ${USER_ID} -g ${GROUP_ID} -M -d /app openedai
RUN chown openedai:openedai /app # for .triton, .config/matplotlib

USER openedai
ENV CLI_COMMAND="python vision.py"
CMD $CLI_COMMAND
