package shortener

import (
	"crypto/rand"
	"sync"
	"sync/atomic"
)

const (
	codeLength = 6
	charset    = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)

// Store holds the URL mappings and click counters in memory.
// All methods are safe for concurrent use.
type Store struct {
	urls   sync.Map // code(string) → url(string)
	clicks sync.Map // code(string) → *atomic.Int64
}

func NewStore() *Store { return &Store{} }

// Shorten generates a unique short code for rawURL and stores the mapping.
func (s *Store) Shorten(rawURL string) (string, error) {
	for {
		code, err := generateCode()
		if err != nil {
			return "", err
		}
		if _, loaded := s.urls.LoadOrStore(code, rawURL); !loaded {
			var c atomic.Int64
			s.clicks.Store(code, &c)
			return code, nil
		}
		// collision — extremely rare with 62^6 ≈ 56B combinations; retry
	}
}

// Get returns the original URL for a short code, or false if unknown.
func (s *Store) Get(code string) (string, bool) {
	v, ok := s.urls.Load(code)
	if !ok {
		return "", false
	}
	return v.(string), true
}

// IncrClick atomically increments the click counter for a code.
// No-ops for unknown codes (analytics worker may race with cleanup).
func (s *Store) IncrClick(code string) {
	if v, ok := s.clicks.Load(code); ok {
		v.(*atomic.Int64).Add(1)
	}
}

// Clicks returns the current click count for a code, or false if unknown.
func (s *Store) Clicks(code string) (int64, bool) {
	v, ok := s.clicks.Load(code)
	if !ok {
		return 0, false
	}
	return v.(*atomic.Int64).Load(), true
}

func generateCode() (string, error) {
	b := make([]byte, codeLength)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	for i := range b {
		b[i] = charset[b[i]%byte(len(charset))]
	}
	return string(b), nil
}
