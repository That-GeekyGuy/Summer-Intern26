module bess.internal/upf-exporter

go 1.22

require (
	github.com/minio/minio-go/v7 v7.0.70
	github.com/parquet-go/parquet-go v0.22.0
)

require (
	github.com/andybalholm/brotli v1.0.5 // indirect
	github.com/dustin/go-humanize v1.0.1 // indirect
	github.com/goccy/go-json v0.10.2 // indirect
	github.com/google/uuid v1.6.0 // indirect
	github.com/klauspost/compress v1.17.8 // indirect
	github.com/klauspost/cpuid/v2 v2.2.6 // indirect
	github.com/mattn/go-runewidth v0.0.9 // indirect
	github.com/minio/md5-simd v1.1.2 // indirect
	github.com/olekukonko/tablewriter v0.0.5 // indirect
	github.com/pierrec/lz4/v4 v4.1.18 // indirect
	github.com/rs/xid v1.5.0 // indirect
	github.com/segmentio/encoding v0.3.6 // indirect
	golang.org/x/crypto v0.21.0 // indirect
	golang.org/x/net v0.23.0 // indirect
	golang.org/x/sys v0.18.0 // indirect
	golang.org/x/text v0.14.0 // indirect
	gopkg.in/ini.v1 v1.67.0 // indirect
)

// Run 'go mod tidy' after first clone to populate go.sum and pull indirect deps.
// In CI, commit go.sum and remove the GONOSUMCHECK workaround from the Dockerfile.
