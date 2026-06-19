// Package temporal provides a client for the STL temporal intelligence sidecar.
// The sidecar runs STL decomposition, traffic regime classification, calendar context,
// and multi-horizon forecasting. All calls are non-blocking — a degraded/unavailable
// sidecar returns empty structs rather than errors, so the main analysis path is unaffected.
package temporal

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"
)

// HourlyStat holds the expected session count for one hour of day.
type HourlyStat struct {
	Hour                  int     `json:"hour"`
	Regime                string  `json:"regime"`
	Confidence            float64 `json:"confidence"`
	ExpectedSessionsMean  float64 `json:"expected_sessions_mean"`
	ExpectedSessionsP10   float64 `json:"expected_sessions_p10"`
	ExpectedSessionsP90   float64 `json:"expected_sessions_p90"`
	HistoricalAnomalyRate float64 `json:"historical_anomaly_rate"`
	Label                 string  `json:"label"`
}

// HotzoneResponse is the 24-hour hourly regime forecast served to the frontend.
type HotzoneResponse struct {
	GeneratedAt         string       `json:"generated_at"`
	DataCoverageDays    float64      `json:"data_coverage_days"`
	Hourly              []HourlyStat `json:"hourly"`
	PeakHours           []int        `json:"peak_hours"`
	TroughHours         []int        `json:"trough_hours"`
	NextPeakInMinutes   *int         `json:"next_peak_in_minutes"`
	NextTroughInMinutes *int         `json:"next_trough_in_minutes"`
	Warning             string       `json:"warning,omitempty"`
}

// rawHotzoneResponse matches the actual sidecar /hotzone JSON (field name differs from frontend).
type rawHotzoneResponse struct {
	GeneratedAt         string       `json:"generated_at"`
	DataCoverageDays    float64      `json:"data_coverage_days"`
	HourlyForecast      []HourlyStat `json:"hourly_forecast"`
	PeakHours           []int        `json:"peak_hours"`
	TroughHours         []int        `json:"trough_hours"`
	NextPeakInMinutes   *int         `json:"next_peak_in_minutes"`
	NextTroughInMinutes *int         `json:"next_trough_in_minutes"`
	Warning             string       `json:"warning,omitempty"`
}

// CalendarContext holds the temporal context for the current moment.
type CalendarContext struct {
	DayOfWeek          string `json:"day_of_week"`
	HourOfDay          int    `json:"hour_of_day"`
	IsWeekend          bool   `json:"is_weekend"`
	IsHoliday          bool   `json:"is_holiday"`
	HolidayName        string `json:"holiday_name,omitempty"`
	IsDayBeforeHoliday bool   `json:"is_day_before_holiday"`
	IsDayAfterHoliday  bool   `json:"is_day_after_holiday"`
	WeekOfMonth        int    `json:"week_of_month"`
}

// CurrentRegime holds the real-time regime classification.
type CurrentRegime struct {
	Regime     string  `json:"regime"`
	Percentile float64 `json:"percentile"`
}

// AnalysisResponse is the flattened temporal analysis served to the frontend.
type AnalysisResponse struct {
	GeneratedAt         string          `json:"generated_at"`
	DataCoverageDays    float64         `json:"data_coverage_days"`
	Calendar            CalendarContext `json:"calendar"`
	CurrentRegime       CurrentRegime   `json:"current_regime"`
	PeakHours           []int           `json:"peak_hours"`
	TroughHours         []int           `json:"trough_hours"`
	MinutesToNextPeak   *int            `json:"minutes_to_next_peak"`
	MinutesToNextTrough *int            `json:"minutes_to_next_trough"`
	Warning             string          `json:"warning,omitempty"`
}

// rawCalendar matches the sidecar's calendar JSON (may include next_holiday sub-object).
type rawCalendar struct {
	DayOfWeek          string  `json:"day_of_week"`
	HourOfDay          int     `json:"hour_of_day"`
	IsWeekend          bool    `json:"is_weekend"`
	IsHoliday          bool    `json:"is_holiday"`
	HolidayName        *string `json:"holiday_name"`
	IsDayBeforeHoliday bool    `json:"is_day_before_holiday"`
	IsDayAfterHoliday  bool    `json:"is_day_after_holiday"`
	WeekOfMonth        int     `json:"week_of_month"`
}

// rawCurrentState maps to the sidecar's "current_state" object.
type rawCurrentState struct {
	Regime     string      `json:"regime"`
	Percentile float64     `json:"percentile"`
	Calendar   rawCalendar `json:"calendar"`
}

// rawHotZones maps to the sidecar's "hot_zones_next_24h" object.
type rawHotZones struct {
	PeakHours           []int `json:"peak_hours"`
	TroughHours         []int `json:"trough_hours"`
	NextPeakInMinutes   *int  `json:"next_peak_in_minutes"`
	NextTroughInMinutes *int  `json:"next_trough_in_minutes"`
}

// rawAnalysis matches the actual sidecar /analysis JSON structure.
type rawAnalysis struct {
	GeneratedAt      string          `json:"generated_at"`
	DataCoverageDays float64         `json:"data_coverage_days"`
	CurrentState     rawCurrentState `json:"current_state"`
	HotZonesNext24h  rawHotZones     `json:"hot_zones_next_24h"`
	Warning          string          `json:"warning,omitempty"`
}

func flattenAnalysis(r rawAnalysis) AnalysisResponse {
	cal := r.CurrentState.Calendar
	holidayName := ""
	if cal.HolidayName != nil {
		holidayName = *cal.HolidayName
	}
	return AnalysisResponse{
		GeneratedAt:      r.GeneratedAt,
		DataCoverageDays: r.DataCoverageDays,
		Calendar: CalendarContext{
			DayOfWeek:          cal.DayOfWeek,
			HourOfDay:          cal.HourOfDay,
			IsWeekend:          cal.IsWeekend,
			IsHoliday:          cal.IsHoliday,
			HolidayName:        holidayName,
			IsDayBeforeHoliday: cal.IsDayBeforeHoliday,
			IsDayAfterHoliday:  cal.IsDayAfterHoliday,
			WeekOfMonth:        cal.WeekOfMonth,
		},
		CurrentRegime: CurrentRegime{
			Regime:     r.CurrentState.Regime,
			Percentile: r.CurrentState.Percentile,
		},
		PeakHours:           r.HotZonesNext24h.PeakHours,
		TroughHours:         r.HotZonesNext24h.TroughHours,
		MinutesToNextPeak:   r.HotZonesNext24h.NextPeakInMinutes,
		MinutesToNextTrough: r.HotZonesNext24h.NextTroughInMinutes,
		Warning:             r.Warning,
	}
}

// Client is a cached HTTP client for the STL temporal sidecar.
type Client struct {
	baseURL    string
	httpClient *http.Client

	mu               sync.Mutex
	cachedAnalysis   *AnalysisResponse
	analysisCachedAt time.Time
	analysisTTL      time.Duration

	cachedHotzone   *HotzoneResponse
	hotzoneCachedAt time.Time
	hotzoneTTL      time.Duration
}

// New creates a Client. baseURL is e.g. "http://stl-sidecar:8085".
func New(baseURL string) *Client {
	return &Client{
		baseURL: baseURL,
		httpClient: &http.Client{
			Timeout: 5 * time.Second,
		},
		analysisTTL: 60 * time.Second,
		hotzoneTTL:  60 * time.Minute,
	}
}

// GetAnalysis returns the full temporal analysis, cached for 60 seconds.
// Returns an empty struct (not an error) if the sidecar is unavailable.
func (c *Client) GetAnalysis(ctx context.Context) (*AnalysisResponse, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if c.cachedAnalysis != nil && time.Since(c.analysisCachedAt) < c.analysisTTL {
		return c.cachedAnalysis, nil
	}

	var raw rawAnalysis
	if err := c.get(ctx, "/analysis", &raw); err != nil {
		return nil, err
	}
	resp := flattenAnalysis(raw)
	c.cachedAnalysis = &resp
	c.analysisCachedAt = time.Now()
	return &resp, nil
}

// GetHotzone returns the 24-hour regime forecast, cached for 1 hour.
// Returns an empty struct (not an error) if the sidecar is unavailable.
func (c *Client) GetHotzone(ctx context.Context) (*HotzoneResponse, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if c.cachedHotzone != nil && time.Since(c.hotzoneCachedAt) < c.hotzoneTTL {
		return c.cachedHotzone, nil
	}

	var raw rawHotzoneResponse
	if err := c.get(ctx, "/hotzone", &raw); err != nil {
		return nil, err
	}
	resp := HotzoneResponse{
		GeneratedAt:         raw.GeneratedAt,
		DataCoverageDays:    raw.DataCoverageDays,
		Hourly:              raw.HourlyForecast,
		PeakHours:           raw.PeakHours,
		TroughHours:         raw.TroughHours,
		NextPeakInMinutes:   raw.NextPeakInMinutes,
		NextTroughInMinutes: raw.NextTroughInMinutes,
		Warning:             raw.Warning,
	}
	c.cachedHotzone = &resp
	c.hotzoneCachedAt = time.Now()
	return &resp, nil
}

func (c *Client) get(ctx context.Context, path string, dest any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+path, nil)
	if err != nil {
		return fmt.Errorf("temporal: new request: %w", err)
	}

	res, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("temporal: %s: %w", path, err)
	}
	defer res.Body.Close()

	if res.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(res.Body, 512))
		return fmt.Errorf("temporal: %s: HTTP %d: %s", path, res.StatusCode, body)
	}

	if err := json.NewDecoder(res.Body).Decode(dest); err != nil {
		return fmt.Errorf("temporal: %s: decode: %w", path, err)
	}
	return nil
}
