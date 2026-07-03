package store

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

// Event is a single anomaly detection record to persist.
type Event struct {
	MetricName            string
	Labels                string   // JSON object
	Timestamp             time.Time
	ObservedValue         float64
	ExpectedValue         *float64 // nil when expected == bound (threshold rule)
	DeviationMagnitude    float64
	RuleName              string
	Severity              string
	EventType             string   // "reactive", "predictive", or "ml"
	ForecastHorizon       string   // e.g. "1h"; empty for non-predictive events
	PredictedCrossingTime *int64   // Unix seconds when trend crosses capacity; nil for non-predictive
	Confidence            *float64 // R² for predictive; RF probability for ml events
	ThresholdConfig       string   // JSON {"capacity":N,"metric":"..."}; empty for non-predictive
	FeatureContributions  string   // JSON [{name,importance}×3]; only for ml events
	RCAReport             string   // JSON RCA report from Brain 2; only for ml events
}

// QueryResult is a row returned from List.
type QueryResult struct {
	ID                    int64     `json:"id"`
	MetricName            string    `json:"metric_name"`
	Labels                string    `json:"labels"`
	Timestamp             time.Time `json:"timestamp"`
	ObservedValue         float64   `json:"observed_value"`
	ExpectedValue         *float64  `json:"expected_value,omitempty"`
	DeviationMagnitude    float64   `json:"deviation_magnitude"`
	RuleName              string    `json:"rule_name"`
	Severity              string    `json:"severity"`
	EventType             string    `json:"event_type"`
	ForecastHorizon       string    `json:"forecast_horizon,omitempty"`
	PredictedCrossingTime *int64    `json:"predicted_crossing_time,omitempty"`
	Confidence            *float64  `json:"confidence,omitempty"`
	ThresholdConfig       string    `json:"threshold_config,omitempty"`
	FeatureContributions  string    `json:"feature_contributions,omitempty"`
	RCAReport             string    `json:"rca_report,omitempty"`
	CreatedAt             time.Time `json:"created_at"`
}

// Store persists anomaly events in SQLite.
type Store struct {
	db *sql.DB
}

// baseSchema defines the original table structure — kept minimal so that
// ALTER TABLE migrations below are the single source of truth for new columns.
const baseSchema = `
CREATE TABLE IF NOT EXISTS anomaly_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name          TEXT    NOT NULL,
    labels               TEXT    NOT NULL,
    timestamp            INTEGER NOT NULL,
    observed_value       REAL    NOT NULL,
    expected_value       REAL,
    deviation_magnitude  REAL    NOT NULL,
    rule_name            TEXT    NOT NULL,
    severity             TEXT    NOT NULL,
    created_at           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anomaly_created_at ON anomaly_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_anomaly_metric     ON anomaly_events(metric_name, created_at DESC);

CREATE TABLE IF NOT EXISTS schema_migrations (
	version INTEGER PRIMARY KEY,
	applied_at INTEGER NOT NULL
);
`

// versionedMigrations defines schema changes sequentially.
var versionedMigrations = []string{
	`ALTER TABLE anomaly_events ADD COLUMN event_type TEXT NOT NULL DEFAULT 'reactive'`,
	`ALTER TABLE anomaly_events ADD COLUMN forecast_horizon TEXT`,
	`ALTER TABLE anomaly_events ADD COLUMN predicted_crossing_time INTEGER`,
	`ALTER TABLE anomaly_events ADD COLUMN confidence REAL`,
	`ALTER TABLE anomaly_events ADD COLUMN threshold_config TEXT`,
	`ALTER TABLE anomaly_events ADD COLUMN feature_contributions TEXT`,
	`ALTER TABLE anomaly_events ADD COLUMN rca_report TEXT`,
	`CREATE INDEX IF NOT EXISTS idx_anomaly_type ON anomaly_events(event_type, created_at DESC)`,
	// DB10: Compound unique index to prevent duplicate identical events.
	`CREATE UNIQUE INDEX IF NOT EXISTS idx_anomaly_uniq ON anomaly_events(metric_name, labels, timestamp, rule_name)`,
}

// Open opens (or creates) the SQLite database at path and applies the schema.
func Open(path string) (*Store, error) {
	if !strings.Contains(path, "?") {
		path += "?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)"
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	if _, err := db.Exec(baseSchema); err != nil {
		db.Close()
		return nil, fmt.Errorf("base schema: %w", err)
	}

	// Apply versioned migrations
	for i, m := range versionedMigrations {
		version := i + 1
		var applied bool
		err := db.QueryRow("SELECT 1 FROM schema_migrations WHERE version = ?", version).Scan(&applied)
		if err == sql.ErrNoRows {
			if _, execErr := db.Exec(m); execErr != nil {
				// Ignore duplicate column errors for columns added before the migration tracker existed
				if !strings.Contains(execErr.Error(), "duplicate column name") {
					db.Close()
					return nil, fmt.Errorf("migration %d: %w", version, execErr)
				}
			}
			_, _ = db.Exec("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", version, time.Now().Unix())
		} else if err != nil {
			db.Close()
			return nil, fmt.Errorf("check migration %d: %w", version, err)
		}
	}
	return &Store{db: db}, nil
}

// Insert persists an anomaly event. Ignores duplicates violating the unique index.
func (s *Store) Insert(ctx context.Context, ev Event) error {
	eventType := ev.EventType
	if eventType == "" {
		eventType = "reactive"
	}
	_, err := s.db.ExecContext(ctx,
		`INSERT OR IGNORE INTO anomaly_events
         (metric_name, labels, timestamp, observed_value, expected_value,
          deviation_magnitude, rule_name, severity, created_at,
          event_type, forecast_horizon, predicted_crossing_time, confidence, threshold_config,
          feature_contributions, rca_report)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		ev.MetricName, ev.Labels, ev.Timestamp.Unix(),
		ev.ObservedValue, ev.ExpectedValue,
		ev.DeviationMagnitude, ev.RuleName, ev.Severity,
		time.Now().Unix(),
		eventType,
		nullStr(ev.ForecastHorizon),
		ev.PredictedCrossingTime,
		ev.Confidence,
		nullStr(ev.ThresholdConfig),
		nullStr(ev.FeatureContributions),
		nullStr(ev.RCAReport),
	)
	return err
}

// UpdateRCAReport sets the rca_report column for the most recent ml event
// matching the given metric_name. Used by the AI evaluator after async Brain 2 RCA.
func (s *Store) UpdateRCAReport(ctx context.Context, metricName, labelsJSON, rcaJSON string) error {
	_, err := s.db.ExecContext(ctx,
		`UPDATE anomaly_events SET rca_report = ?
         WHERE id = (
             SELECT id FROM anomaly_events
             WHERE metric_name = ? AND labels = ? AND event_type = 'ml'
             ORDER BY created_at DESC LIMIT 1
         )`,
		rcaJSON, metricName, labelsJSON,
	)
	return err
}

// List returns anomaly events.
// Use 'since' (created_at >= since) for polling new events.
// Use 'cursorID' (id < cursorID) and 'limit' for backward pagination.
func (s *Store) List(ctx context.Context, since time.Time, cursorID int64, limit int, metric, severity, eventType string) ([]QueryResult, error) {
	q := `SELECT id, metric_name, labels, timestamp, observed_value, expected_value,
                 deviation_magnitude, rule_name, severity,
                 event_type, forecast_horizon, predicted_crossing_time, confidence, threshold_config,
                 feature_contributions, rca_report, created_at
          FROM anomaly_events WHERE 1=1`
	var args []any
	
	if since.Unix() > 0 {
		q += " AND created_at >= ?"
		args = append(args, since.Unix())
	}
	if cursorID > 0 {
		q += " AND id < ?"
		args = append(args, cursorID)
	}
	if metric != "" {
		q += " AND metric_name = ?"
		args = append(args, metric)
	}
	if severity != "" {
		q += " AND severity = ?"
		args = append(args, severity)
	}
	if eventType != "" {
		q += " AND event_type = ?"
		args = append(args, eventType)
	}
	
	if limit <= 0 || limit > 1000 {
		limit = 1000
	}
	q += " ORDER BY id DESC LIMIT ?"
	args = append(args, limit)

	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []QueryResult
	for rows.Next() {
		var r QueryResult
		var ts, ca int64
		var fh, threshCfg, featContrib, rcaReport sql.NullString
		var pct sql.NullInt64
		var conf sql.NullFloat64
		if err := rows.Scan(
			&r.ID, &r.MetricName, &r.Labels, &ts,
			&r.ObservedValue, &r.ExpectedValue,
			&r.DeviationMagnitude, &r.RuleName, &r.Severity,
			&r.EventType, &fh, &pct, &conf, &threshCfg,
			&featContrib, &rcaReport, &ca,
		); err != nil {
			return nil, err
		}
		r.Timestamp = time.Unix(ts, 0)
		r.CreatedAt = time.Unix(ca, 0)
		if fh.Valid {
			r.ForecastHorizon = fh.String
		}
		if pct.Valid {
			x := pct.Int64
			r.PredictedCrossingTime = &x
		}
		if conf.Valid {
			r.Confidence = &conf.Float64
		}
		if threshCfg.Valid {
			r.ThresholdConfig = threshCfg.String
		}
		if featContrib.Valid {
			r.FeatureContributions = featContrib.String
		}
		if rcaReport.Valid {
			r.RCAReport = rcaReport.String
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// HasRecentPrediction returns true if a predictive event for the given metric
// and label set was stored within the last `within` duration.
func (s *Store) HasRecentPrediction(ctx context.Context, metricName, labelsJSON string, within time.Duration) bool {
	since := time.Now().Add(-within).Unix()
	var count int
	err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM anomaly_events
         WHERE metric_name = ? AND labels = ? AND event_type = 'predictive' AND created_at >= ?`,
		metricName, labelsJSON, since).Scan(&count)
	return err == nil && count > 0
}

// HasRecentEvent returns true if an event of the given type for the given metric
// and label set was stored within the last `within` duration.
func (s *Store) HasRecentEvent(ctx context.Context, metricName, labelsJSON, eventType string, within time.Duration) bool {
	since := time.Now().Add(-within).Unix()
	var count int
	err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM anomaly_events
         WHERE metric_name = ? AND labels = ? AND event_type = ? AND created_at >= ?`,
		metricName, labelsJSON, eventType, since).Scan(&count)
	return err == nil && count > 0
}

// Prune deletes events older than retentionDays. Returns the row count removed.
func (s *Store) Prune(ctx context.Context, retentionDays int) (int64, error) {
	cutoff := time.Now().AddDate(0, 0, -retentionDays).Unix()
	res, err := s.db.ExecContext(ctx, `DELETE FROM anomaly_events WHERE created_at < ?`, cutoff)
	if err != nil {
		return 0, err
	}
	return res.RowsAffected()
}

// Close closes the underlying database connection.
func (s *Store) Close() error { return s.db.Close() }

func nullStr(s string) any {
	if s == "" {
		return nil
	}
	return s
}
