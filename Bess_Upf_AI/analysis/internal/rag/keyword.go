package rag

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// KeywordRetriever performs case-insensitive keyword search over markdown files
// in a directory tree. It is a lightweight RAG stub — no embeddings required.
type KeywordRetriever struct {
	docs []doc
}

type doc struct {
	source   string
	content  string
	paragraphs []string
}

// NewKeywordRetriever loads all *.md files from docsDir recursively.
func NewKeywordRetriever(docsDir string) (*KeywordRetriever, error) {
	var docs []doc
	err := filepath.WalkDir(docsDir, func(path string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(path, ".md") {
			return err
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return nil // skip unreadable files
		}
		content := string(data)
		rel, _ := filepath.Rel(docsDir, path)
		docs = append(docs, doc{
			source:     rel,
			content:    content,
			paragraphs: splitParagraphs(content),
		})
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &KeywordRetriever{docs: docs}, nil
}

// Retrieve returns up to topK snippets containing the most query keywords.
func (r *KeywordRetriever) Retrieve(query string, topK int) []Snippet {
	words := tokenize(query)
	if len(words) == 0 || len(r.docs) == 0 {
		return nil
	}

	type scored struct {
		Snippet
		score float64
	}
	var results []scored

	for _, d := range r.docs {
		for _, para := range d.paragraphs {
			if strings.TrimSpace(para) == "" {
				continue
			}
			lower := strings.ToLower(para)
			hits := 0
			for _, w := range words {
				if strings.Contains(lower, w) {
					hits++
				}
			}
			if hits == 0 {
				continue
			}
			results = append(results, scored{
				Snippet: Snippet{Source: d.source, Content: strings.TrimSpace(para)},
				score:   float64(hits) / float64(len(words)),
			})
		}
	}

	sort.Slice(results, func(i, j int) bool { return results[i].score > results[j].score })

	if topK > len(results) {
		topK = len(results)
	}
	out := make([]Snippet, topK)
	for i := range out {
		out[i] = results[i].Snippet
		out[i].Score = results[i].score
	}
	return out
}

func tokenize(s string) []string {
	lower := strings.ToLower(s)
	words := strings.FieldsFunc(lower, func(r rune) bool {
		return !('a' <= r && r <= 'z') && !('0' <= r && r <= '9') && r != '_'
	})
	// Deduplicate and drop very short words.
	seen := make(map[string]bool)
	var out []string
	for _, w := range words {
		if len(w) >= 3 && !seen[w] {
			seen[w] = true
			out = append(out, w)
		}
	}
	return out
}

func splitParagraphs(content string) []string {
	return strings.Split(content, "\n\n")
}
