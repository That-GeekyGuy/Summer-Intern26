package store

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	_ "modernc.org/sqlite"
)

// AuditEntry records one interaction with the LLM chat API.
type AuditEntry struct {
	SessionID       string
	UserMessage     string
	ToolName        string
	ToolArgs        string
	ValidationError string
	ExecutionStatus string
	RowCount        int
	CreatedAt       time.Time
}

// AuditLog persists query audit records to SQLite.
type AuditLog struct {
	db *sql.DB
}

const auditSchema = `
CREATE TABLE IF NOT EXISTS query_audit (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT    NOT NULL,
    user_message     TEXT    NOT NULL,
    tool_name        TEXT    NOT NULL DEFAULT '',
    tool_args        TEXT    NOT NULL DEFAULT '',
    validation_error TEXT    NOT NULL DEFAULT '',
    execution_status TEXT    NOT NULL DEFAULT '',
    row_count        INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_session   ON query_audit(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_created   ON query_audit(created_at DESC);
`

// OpenAuditLog opens (or creates) the SQLite audit database at path.
func OpenAuditLog(path string) (*AuditLog, error) {
	// Enable WAL mode and busy_timeout in the connection string
	if !strings.Contains(path, "?") {
		path += "?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)"
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("open audit sqlite: %w", err)
	}
	// WAL mode allows concurrent readers while a writer holds the lock.
	// Required because the audit endpoint reads concurrently with LLM writes.
	if _, err := db.Exec("PRAGMA journal_mode=WAL"); err != nil {
		db.Close()
		return nil, fmt.Errorf("set WAL mode: %w", err)
	}
	if _, err := db.Exec(auditSchema); err != nil {
		db.Close()
		return nil, fmt.Errorf("migrate audit schema: %w", err)
	}
	return &AuditLog{db: db}, nil
}

// AuditRow is the read-side projection of query_audit — safe to serialise to JSON.
// user_message and tool_args are truncated to prevent log scraping of user input.
type AuditRow struct {
	ID              int64  `json:"id"`
	SessionID       string `json:"session_id"`
	UserMessage     string `json:"user_message"` // truncated to 512 chars
	ToolName        string `json:"tool_name"`
	ToolArgs        string `json:"tool_args"` // truncated to 512 chars
	ValidationError string `json:"validation_error,omitempty"`
	ExecutionStatus string `json:"execution_status"`
	RowCount        int    `json:"row_count"`
	CreatedAt       int64  `json:"created_at"` // Unix seconds
}

// Query returns recent audit entries in reverse-chronological order.
// toolName is an optional filter (empty = all). since is an optional Unix timestamp lower bound.
func (a *AuditLog) Query(ctx context.Context, limit int, since int64, toolName string) ([]AuditRow, int, error) {
	if limit <= 0 || limit > 500 {
		limit = 200
	}
	args := []any{since}
	filter := "WHERE created_at >= ?"
	if toolName != "" {
		filter += " AND tool_name = ?"
		args = append(args, toolName)
	}

	var total int
	if err := a.db.QueryRowContext(ctx,
		"SELECT COUNT(*) FROM query_audit "+filter, args...).Scan(&total); err != nil {
		return nil, 0, fmt.Errorf("count audit: %w", err)
	}

	rows, err := a.db.QueryContext(ctx,
		"SELECT id, session_id, user_message, tool_name, tool_args, "+
			"validation_error, execution_status, row_count, created_at "+
			"FROM query_audit "+filter+
			" ORDER BY created_at DESC LIMIT ?",
		append(args, limit)...)
	if err != nil {
		return nil, 0, fmt.Errorf("query audit: %w", err)
	}
	defer rows.Close()

	var out []AuditRow
	for rows.Next() {
		var r AuditRow
		if err := rows.Scan(&r.ID, &r.SessionID, &r.UserMessage, &r.ToolName, &r.ToolArgs,
			&r.ValidationError, &r.ExecutionStatus, &r.RowCount, &r.CreatedAt); err != nil {
			return nil, 0, err
		}
		r.UserMessage = truncate(r.UserMessage, 512)
		r.ToolArgs    = truncate(r.ToolArgs, 512)
		out = append(out, r)
	}
	return out, total, rows.Err()
}

func truncate(s string, n int) string {
	runes := []rune(s)
	if len(runes) <= n {
		return s
	}
	return string(runes[:n]) + "…"
}

// Log records an audit entry. Call for every tool invocation and every chat turn.
func (a *AuditLog) Log(ctx context.Context, entry AuditEntry) error {
	if entry.CreatedAt.IsZero() {
		entry.CreatedAt = time.Now()
	}
	_, err := a.db.ExecContext(ctx,
		`INSERT INTO query_audit
         (session_id, user_message, tool_name, tool_args,
          validation_error, execution_status, row_count, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		entry.SessionID, entry.UserMessage, entry.ToolName, entry.ToolArgs,
		entry.ValidationError, entry.ExecutionStatus, entry.RowCount,
		entry.CreatedAt.Unix(),
	)
	return err
}

// Close closes the underlying database.
func (a *AuditLog) Close() error { return a.db.Close() }
