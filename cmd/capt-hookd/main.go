package main

import (
	"os"

	"github.com/yasyf/captain-hook/internal/hookd"
)

func main() {
	os.Exit(hookd.Main(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}
