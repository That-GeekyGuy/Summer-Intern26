FROM golang:latest AS builder

WORKDIR /app
COPY go.mod ./
RUN go mod download
COPY . .

RUN CGO_ENABLED=0 GOOS=linux go build -o udp_listener .

FROM alpine:3.19
COPY --from=builder /app/udp_listener /udp_listener
RUN chmod +x /udp_listener
CMD ["/udp_listener"]
