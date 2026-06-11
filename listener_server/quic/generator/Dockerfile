FROM golang:latest AS builder

WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .

RUN CGO_ENABLED=0 GOOS=linux go build -o quic_generator .

FROM alpine:3.19
COPY --from=builder /app/quic_generator /quic_generator
RUN chmod +x /quic_generator
CMD ["/quic_generator"]
