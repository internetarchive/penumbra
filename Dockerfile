FROM python:3.12-slim

RUN pip install uv

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/

# Install dependencies directly from PyPI, bypassing the lock file
RUN uv pip install --system .

# Install Chromium and its system dependencies
RUN playwright install --with-deps chromium

# Browser is already installed; skip the runtime install check
ENV PENUMBRA_INSTALL_PLAYWRIGHT=false

CMD ["penumbra"]
