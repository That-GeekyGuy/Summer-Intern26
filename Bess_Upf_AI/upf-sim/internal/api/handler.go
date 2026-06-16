package api

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"time"

	"bess.internal/upf-sim/internal/sim"
)

// ScenarioRequest is the POST /scenario body.
type ScenarioRequest struct {
	Mode     string `json:"mode"`
	Duration string `json:"duration"` // e.g. "10m", "0" or "" = indefinite
}

// ScenarioResponse is the GET/POST /scenario response.
type ScenarioResponse struct {
	Mode             string  `json:"mode"`
	StartedAt        string  `json:"started_at"`
	UptimeSeconds    float64 `json:"uptime_seconds"`
	Duration         string  `json:"duration,omitempty"`
	RemainingSeconds float64 `json:"remaining_seconds,omitempty"`
}

var validModes = map[sim.Mode]bool{
	sim.ModeNormal:          true,
	sim.ModeSessionSpike:    true,
	sim.ModeSessionDrop:     true,
	sim.ModePacketDropSurge: true,
	sim.ModeAsymmetric:      true,
	sim.ModeFlatline:        true,
}

// HandleScenarioGet returns the current scenario state as JSON.
func HandleScenarioGet(engine *sim.Engine) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s := engine.CurrentState()
		resp := buildResponse(s, time.Now())
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp) //nolint:errcheck
	}
}

// HandleScenarioPost switches the active scenario mode.
func HandleScenarioPost(engine *sim.Engine, log *slog.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var req ScenarioRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, `{"error":"invalid JSON"}`, http.StatusBadRequest)
			return
		}

		mode := sim.Mode(req.Mode)
		if !validModes[mode] {
			http.Error(w, `{"error":"unknown mode"}`, http.StatusBadRequest)
			return
		}

		var dur time.Duration
		if req.Duration != "" && req.Duration != "0" {
			var err error
			dur, err = time.ParseDuration(req.Duration)
			if err != nil {
				http.Error(w, `{"error":"invalid duration"}`, http.StatusBadRequest)
				return
			}
		}

		engine.SetMode(mode, dur)
		log.Info("scenario set via API", "mode", string(mode), "duration", dur)

		s := engine.CurrentState()
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(buildResponse(s, time.Now())) //nolint:errcheck
	}
}

func buildResponse(s sim.ScenarioState, now time.Time) ScenarioResponse {
	resp := ScenarioResponse{
		Mode:          string(s.Mode),
		StartedAt:     s.StartedAt.Format(time.RFC3339),
		UptimeSeconds: now.Sub(s.StartedAt).Seconds(),
	}
	if s.Duration > 0 {
		resp.Duration = s.Duration.String()
		remaining := s.StartedAt.Add(s.Duration).Sub(now).Seconds()
		if remaining < 0 {
			remaining = 0
		}
		resp.RemainingSeconds = remaining
	}
	return resp
}
