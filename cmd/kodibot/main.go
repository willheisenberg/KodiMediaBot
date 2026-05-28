package main

import (
	"log"
	"os"

	"github.com/willheisenberg/KodiMediaBot/internal/config"
	"github.com/willheisenberg/KodiMediaBot/internal/telegram"
)

func main() {
	cfg := config.Get()

	logLevel := log.LstdFlags
	if cfg.DebugWS {
		logLevel = log.LstdFlags | log.Lshortfile
	}
	log.SetOutput(os.Stdout)
	log.SetFlags(logLevel)
	log.SetPrefix("")

	log.Printf("Starting KodiMediaBot (Go) token=***%s", cfg.TGToken[len(cfg.TGToken)-4:])
	telegram.Run(cfg.TGToken)
}
