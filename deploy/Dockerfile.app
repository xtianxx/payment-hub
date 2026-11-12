# T073 fixed-input application image. Build from the repository root:
#   docker build --platform linux/amd64 \
#     --label payment-hub.source-manifest-digest=<digest> \
#     -f deploy/Dockerfile.app .
# The source manifest label is injected by the parent build command; this
# Dockerfile does not hardcode a source SHA or registry digest.
#
# CGO_ENABLED=0 produces a static linux/amd64 binary for the image. Host
# integration tests use CGO_ENABLED=1; this is intentional and does not create
# a second application artifact. The same image/binary runs in normal and test mode;
# only runtime environment selects test mode.

FROM docker.io/library/golang:1.27.4-bookworm@sha256:4fa7c968ba93f1635670522dbb01a2c78150443e1385746924566079f36570ea AS build

ENV CGO_ENABLED=0
ENV GOTOOLCHAIN=local
ENV GOENV=off
ENV GOWORK=off
ENV GOTELEMETRY=off
ENV GOFLAGS=
ENV GOPROXY=https://proxy.golang.org
ENV GOSUMDB=sum.golang.org

RUN go telemetry off

WORKDIR /src

COPY go.mod go.sum /src/
RUN go mod download

COPY cmd/payment-hub/main.go /src/cmd/payment-hub/main.go
COPY internal/app/app.go internal/app/business.go internal/app/config.go internal/app/lifecycle.go internal/app/logging.go internal/app/testmode.go /src/internal/app/
COPY internal/httpapi/auth.go internal/httpapi/payments.go internal/httpapi/readyz.go internal/httpapi/request.go internal/httpapi/response.go internal/httpapi/router.go /src/internal/httpapi/
COPY internal/mock/hooks.go internal/mock/query.go internal/mock/transaction.go /src/internal/mock/
COPY internal/payment/errors.go internal/payment/identity.go internal/payment/ports.go internal/payment/query.go internal/payment/query_types.go internal/payment/result.go internal/payment/submit.go internal/payment/submit_validation.go internal/payment/types.go internal/payment/validation.go /src/internal/payment/
COPY internal/postgres/hooks.go internal/postgres/lookup.go internal/postgres/orders.go internal/postgres/results.go /src/internal/postgres/
COPY internal/testcontrol/client.go internal/testcontrol/protocol.go /src/internal/testcontrol/

RUN mkdir -p /out
RUN go build -mod=readonly -trimpath -o /out/payment-hub ./cmd/payment-hub

FROM docker.io/library/debian:bookworm-slim@sha256:c67e92c8099ba22853ab4cfda7cfb494d253d6012ca82dee98ba8976772af018

ENV TZ=UTC
ENV HOME=/tmp

COPY --from=build /out/payment-hub /usr/local/bin/payment-hub
RUN chmod 0755 /usr/local/bin/payment-hub

EXPOSE 8080
STOPSIGNAL SIGTERM
USER 65534:65534
ENTRYPOINT ["/usr/local/bin/payment-hub"]
