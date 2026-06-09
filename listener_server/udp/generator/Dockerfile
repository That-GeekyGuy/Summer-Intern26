FROM golang:latest AS builder

WORKDIR /app
COPY go.mod ./
RUN go mod download
COPY . .

RUN CGO_ENABLED=0 GOOS=linux go build -o udp_sender .

FROM alpine:3.19
COPY --from=builder /app/udp_sender /udp_sender
RUN chmod +x /udp_sender
CMD ["/udp_sender"]
