FROM golang:latest AS builder

WORKDIR /app
COPY go.mod ./
RUN go mod download
COPY . .

RUN CGO_ENABLED=0 GOOS=linux go build -o tcp_sender .

FROM alpine:3.19
COPY --from=builder /app/tcp_sender /tcp_sender
RUN chmod +x /tcp_sender
CMD ["/tcp_sender"]