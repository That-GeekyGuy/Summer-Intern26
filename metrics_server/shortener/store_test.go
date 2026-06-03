package shortener_test

import (
	"strings"
	"testing"

	"metrics-query/shortener"
)

func TestStore_ShortenAndGet(t *testing.T) {
	s := shortener.NewStore()
	code, err := s.Shorten("https://example.com")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(code) != 6 {
		t.Fatalf("expected 6-char code, got %q", code)
	}

	got, ok := s.Get(code)
	if !ok {
		t.Fatal("expected code to exist in store")
	}
	if got != "https://example.com" {
		t.Fatalf("expected original URL, got %q", got)
	}
}

func TestStore_GetUnknownCode(t *testing.T) {
	s := shortener.NewStore()
	_, ok := s.Get("xxxxxx")
	if ok {
		t.Fatal("expected false for unknown code")
	}
}

func TestStore_IncrAndClicks(t *testing.T) {
	s := shortener.NewStore()
	code, _ := s.Shorten("https://example.com")

	s.IncrClick(code)
	s.IncrClick(code)

	n, ok := s.Clicks(code)
	if !ok {
		t.Fatal("expected clicks entry to exist")
	}
	if n != 2 {
		t.Fatalf("expected 2 clicks, got %d", n)
	}
}

func TestStore_CodeIsBase62(t *testing.T) {
	s := shortener.NewStore()
	const charset = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
	for i := 0; i < 20; i++ {
		code, _ := s.Shorten("https://example.com/" + strings.Repeat("x", i))
		for _, c := range code {
			if !strings.ContainsRune(charset, c) {
				t.Fatalf("code %q contains non-base62 char %q", code, c)
			}
		}
	}
}
