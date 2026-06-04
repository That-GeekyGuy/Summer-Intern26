FROM golang:latest AS builder

WORKDIR /app
COPY go.mod ./
RUN go mod download
COPY . .

RUN CGO_ENABLED=0 GOOS=linux go build -o tcp_listener .

FROM alpine:3.19
COPY --from=builder /app/tcp_listener /tcp_listener
RUN chmod +x /tcp_listener
CMD ["/tcp_listener"]