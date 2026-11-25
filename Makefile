.PHONY: help test test-offline lint install uninstall clean distclean

help:
	@echo "musicctl - Lightweight Standalone YouTube Music CLI & TUI Player"
	@echo ""
	@echo "Usage:"
	@echo "  make lint          - Validate shell syntax, desktop entry, and shellcheck"
	@echo "  make test          - Run full verification test suite (offline + live)"
	@echo "  make test-offline  - Run offline verification suite without network calls"
	@echo "  make install       - Install musicctl, completions, and desktop entry"
	@echo "  make uninstall     - Uninstall musicctl from user prefix"
	@echo "  make clean         - Clean packaging artifacts"
	@echo "  make distclean     - Deep clean runtime caches and temporary logs"

lint:
	@set -e; \
	for file in bin/musicctl install.sh tests/test_musicctl.sh completions/musicctl.bash; do \
		bash -n "$$file"; \
	done; \
	python3 -m py_compile lib/common.py lib/tui.py tests/test_tui.py; \
	if command -v desktop-file-validate >/dev/null 2>&1; then \
		desktop-file-validate musicctl.desktop; \
	fi; \
	if command -v shellcheck >/dev/null 2>&1 && shellcheck --version >/dev/null 2>&1; then \
		shellcheck -s bash -S warning bin/musicctl install.sh tests/test_musicctl.sh; \
	elif [ "$${STRICT_LINT:-0}" = "1" ]; then \
		echo "[FAIL] STRICT_LINT=1 requires working shellcheck in PATH" >&2; \
		exit 1; \
	else \
		echo "[INFO] shellcheck unavailable or non-functional; syntax lint passed"; \
	fi

test:
	./tests/test_musicctl.sh < /dev/null
	python3 -m unittest discover -s tests -p "test_*.py" -v

test-offline:
	MUSICCTL_OFFLINE_TESTS=1 ./tests/test_musicctl.sh < /dev/null
	python3 -m unittest discover -s tests -p "test_*.py" -v

install:
	./install.sh

uninstall:
	./install.sh --uninstall

clean:
	rm -rf packaging/arch/*.pkg.tar* packaging/arch/pkg packaging/arch/src dist build

distclean: clean
	rm -rf $${XDG_CACHE_HOME:-$$HOME/.cache}/musicctl $${XDG_RUNTIME_DIR:-/tmp}/musicctl.sock
