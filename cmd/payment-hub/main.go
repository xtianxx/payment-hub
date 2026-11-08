package main

import (
	"context"
	"os"
	"os/signal"
	"syscall"

	"payment-hub/internal/app"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := app.Run(ctx, os.LookupEnv, os.Stdout); err != nil {
		os.Exit(1)
	}
}
