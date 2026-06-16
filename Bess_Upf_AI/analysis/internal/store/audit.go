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
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("open audit sqlite: %w", err)
	}
	db.SetMaxOpenConns(1)
	if _, err := db.Exec(auditSchema); err != nil {
		db.Close()
		return nil, fmt.Errorf("migrate audit schema: %w", err)
	}
	return &AuditLog{db: db}, nil
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
