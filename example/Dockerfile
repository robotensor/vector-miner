FROM node:24-slim
ARG VECTOR_SIM_GIT=https://github.com/robotensor/vector-sim
ARG VECTOR_SIM_REF=main
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "$VECTOR_SIM_REF" "$VECTOR_SIM_GIT" /vector-sim && rm -rf /vector-sim/.git \
    && cd /vector-sim/js && npm ci --omit=dev --no-audit --no-fund
WORKDIR /x/example
COPY package.json package-lock.json solve.mjs ./
RUN npm ci --omit=dev --no-audit --no-fund
ENV VECTOR_SIM_BUNDLE=/bundle HOME=/tmp
USER 65534:65534
ENTRYPOINT ["node", "solve.mjs"]
