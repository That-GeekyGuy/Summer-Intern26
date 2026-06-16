// Command upf-eval sends adversarial prompts to the UPF analysis chat endpoint
// and asserts the PromQL validator blocks dangerous queries.
//
// Usage:
//
//	go run ./eval/ --url https://localhost --user admin --pass secret
//	go run ./eval/ --url https://monitor.example.com --output report.md
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"
)

func main() {
	url := flag.String("url", "https://localhost", "Base URL of the analysis service (e.g. https://monitor.example.com)")
	user := flag.String("user", "", "HTTP Basic Auth username")
	pass := flag.String("pass", "", "HTTP Basic Auth password")
	output := flag.String("output", "", "Write Markdown report to this file (default: stdout)")
	timeout := flag.Duration("timeout", 90*time.Second, "Per-request timeout (LLM calls can be slow)")
	insecure := flag.Bool("insecure", false, "Skip TLS certificate verification (for dev self-signed certs)")
	flag.Parse()

	if *url == "" {
		fmt.Fprintln(os.Stderr, "error: --url is required")
		flag.Usage()
		os.Exit(1)
	}

	h := newHarness(*url, *user, *pass, *timeout, *insecure)
	cases := AllCases()

	fmt.Fprintf(os.Stderr, "Running %d adversarial test cases against %s\n", len(cases), *url)

	start := time.Now()
	ctx := context.Background()
	results := h.Run(ctx, cases)
	elapsed := time.Since(start)

	// Print progress to stderr so stdout stays clean for the report.
	passed, failed := 0, 0
	for i, r := range results {
		icon := "✓"
		if !r.Pass {
			icon = "✗"
			failed++
		} else {
			passed++
		}
		fmt.Fprintf(os.Stderr, "  [%d/%d] %s %s (got %s)\n",
			i+1, len(cases), icon, r.Case.ID, r.Got)
	}
	fmt.Fprintf(os.Stderr, "\nResult: %d passed, %d failed in %s\n",
		passed, failed, elapsed.Round(time.Millisecond))

	report := Report(results, *url, elapsed)

	if *output != "" {
		if err := os.WriteFile(*output, []byte(report), 0644); err != nil {
			fmt.Fprintf(os.Stderr, "error writing report: %v\n", err)
			os.Exit(1)
		}
		fmt.Fprintf(os.Stderr, "Report written to %s\n", *output)
	} else {
		fmt.Print(report)
	}

	if failed > 0 {
		os.Exit(1)
	}
}
