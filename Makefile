HELPER_VERSION ?= 0.0.0

.PHONY: all build test python-build python-test go-build go-test tutorial-build helper-build helper-test

all: build test

build: python-build go-build tutorial-build helper-build

test: python-test go-test helper-test

python-build:
	uv build

python-test: tutorial-build
	uv run pytest

go-build:
	go build ./...

go-test:
	go test -race -count=1 ./...

tutorial-build:
	uv run python docs/scripts/build_emulator.py

helper-build:
	command -v xcodegen >/dev/null 2>&1 || brew install xcodegen
	helper/scripts/build-capt-hookd.sh $(HELPER_VERSION) helper/Generated/capt-hookd tests/fixtures/capt-hookd
	cd helper && ./gen-version-xcconfig.sh $(HELPER_VERSION) && xcodegen generate

helper-test: helper-build
	cd helper && GITHUB_REF_NAME=v$(HELPER_VERSION) xcodebuild test -project CaptainHook.xcodeproj -scheme CaptainHook \
		-destination 'platform=macOS' -derivedDataPath build CODE_SIGNING_ALLOWED=NO
