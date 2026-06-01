package fetcher

import (
	"io"
	"net/http"
)

func FetchRaw(targetURL string) (string, error) {
	resp, err := http.Get(targetURL + "/metrics")
	if err != nil {
		return "", err
	}

	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}
	return string(body), nil
}
