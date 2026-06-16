// Package store defines the ObjectStore interface and provides a MinIO
// implementation. The interface is kept minimal so tests can substitute
// an in-memory fake without spinning up real infrastructure.
package store

import (
	"context"
	"io"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

// ObjectStore is the S3-like interface used by the export pipeline.
type ObjectStore interface {
	// ObjectExists reports whether bucket/key exists.
	ObjectExists(ctx context.Context, bucket, key string) (bool, error)

	// PutObject writes all bytes from r to bucket/key, replacing any existing
	// object. size must match the actual byte count in r; pass -1 to stream
	// without a known size (less efficient).
	PutObject(ctx context.Context, bucket, key string, r io.Reader, size int64, contentType string) error
}

// MinIOConfig holds connection parameters for a MinIO (S3-compatible) server.
type MinIOConfig struct {
	Endpoint  string // host:port, e.g. "minio:9000"
	AccessKey string
	SecretKey string
	UseSSL    bool
}

// MinIOStore implements ObjectStore backed by a MinIO server.
type MinIOStore struct {
	client *minio.Client
}

// NewMinIOStore dials the MinIO endpoint and returns a ready store.
// It does not verify bucket existence; call ObjectExists or PutObject to
// surface connectivity errors at operation time.
func NewMinIOStore(cfg MinIOConfig) (*MinIOStore, error) {
	c, err := minio.New(cfg.Endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(cfg.AccessKey, cfg.SecretKey, ""),
		Secure: cfg.UseSSL,
	})
	if err != nil {
		return nil, err
	}
	return &MinIOStore{client: c}, nil
}

func (m *MinIOStore) ObjectExists(ctx context.Context, bucket, key string) (bool, error) {
	_, err := m.client.StatObject(ctx, bucket, key, minio.StatObjectOptions{})
	if err != nil {
		if minio.ToErrorResponse(err).Code == "NoSuchKey" {
			return false, nil
		}
		return false, err
	}
	return true, nil
}

func (m *MinIOStore) PutObject(ctx context.Context, bucket, key string, r io.Reader, size int64, contentType string) error {
	_, err := m.client.PutObject(ctx, bucket, key, r, size, minio.PutObjectOptions{
		ContentType: contentType,
	})
	return err
}
