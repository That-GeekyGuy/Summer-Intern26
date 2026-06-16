package rag

// Snippet is a chunk of document text retrieved for a query.
type Snippet struct {
	Source  string // file or section name
	Content string
	Score   float64
}

// Retriever is the interface implemented by all RAG backends.
type Retriever interface {
	// Retrieve returns the topK most relevant snippets for the given query.
	Retrieve(query string, topK int) []Snippet
}
