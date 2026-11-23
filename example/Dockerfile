FROM node:24-slim
ARG VECTOR_SIM_GIT=https://github.com/robotensor/vector-sim
ARG VECTOR_SIM_REF=v0.1.0
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN git init -q /vector-sim && cd /vector-sim \
    && git fetch -q --depth 1 "$VECTOR_SIM_GIT" "$VECTOR_SIM_REF" && git checkout -q FETCH_HEAD && rm -rf .git \
    && cd js && npm ci --omit=dev --no-audit --no-fund
WORKDIR /x/example
COPY package.json package-lock.json *.mjs ./
RUN npm ci --omit=dev --no-audit --no-fund
ENV VECTOR_SIM_BUNDLE=/bundle HOME=/tmp
USER 65534:65534
ENTRYPOINT ["node", "solve.mjs"]
