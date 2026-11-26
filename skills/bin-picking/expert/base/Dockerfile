# The miner base image: Node 24, @vector/sim at the harness's version, and a placeholder expert.
# Build it with build.sh (it passes vector-sim, exported at the pinned tag, as the `vector-sim` build context).
FROM node:24-slim
COPY --from=vector-sim js /vector-sim/js
RUN cd /vector-sim/js && npm ci --omit=dev --no-audit --no-fund
WORKDIR /expert
RUN npm init -y >/dev/null && npm pkg set type=module && npm install --no-audit --no-fund /vector-sim/js
COPY expert.mjs ./
ENV VECTOR_SIM_BUNDLE=/bundle HOME=/tmp
USER 65534:65534
ENTRYPOINT ["node", "/expert/expert.mjs"]
