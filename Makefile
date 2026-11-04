ARCH ?= x86_64

BUILD_DIR := build/$(ARCH)
BIN       := $(BUILD_DIR)/zfind
OBJ       := $(BUILD_DIR)/zfind.o
SRC       := src/$(ARCH)/zfind.asm
RELEASE_BIN := $(BUILD_DIR)/zfind.release
SERIAL_BUILD_DIR ?= $(BUILD_DIR)-serial
SERIAL_BIN := $(SERIAL_BUILD_DIR)/zfind
SERIAL_OBJ := $(SERIAL_BUILD_DIR)/zfind.o
UNKNOWN_BUILD_DIR ?= $(BUILD_DIR)-unknown
UNKNOWN_BIN := $(UNKNOWN_BUILD_DIR)/zfind
UNKNOWN_OBJ := $(UNKNOWN_BUILD_DIR)/zfind.o
RANGE_BUILD_DIR ?= $(BUILD_DIR)-range
RANGE_BIN := $(RANGE_BUILD_DIR)/zfind
RANGE_OBJ := $(RANGE_BUILD_DIR)/zfind.o

# ---------- architecture-specific settings ----------
ifeq ($(ARCH),x86_64)
  AS      := nasm
  ASFLAGS := -f elf64 -g
  LDFLAGS :=
else ifeq ($(ARCH),aarch64)
  # Placeholder for later
  AS      := as
  ASFLAGS := -g
  LDFLAGS :=
  $(error aarch64 support not implemented yet)
else
  $(error Unsupported ARCH=$(ARCH). Supported: x86_64)
endif

# ---------- targets ----------
.PHONY: all run clean testdata test test-quiet release

all: $(BIN)

$(BUILD_DIR):
	mkdir -p $@

$(OBJ): $(SRC) | $(BUILD_DIR)
	$(AS) $(ASFLAGS) $< -o $@

$(BIN): $(OBJ)
	ld $(LDFLAGS) -o $@ $<

# Production artifact: a stripped copy of $(BIN). $(BIN) keeps its symbols so
# it can still be disassembled with source line numbers (objdump -d -l), while
# the copy that gets distributed carries no .symtab or .debug_* sections.
release: $(RELEASE_BIN)

$(RELEASE_BIN): $(BIN)
	strip --strip-all -o $@ $<

$(SERIAL_BUILD_DIR):
	mkdir -p $@

$(SERIAL_OBJ): $(SRC) | $(SERIAL_BUILD_DIR)
	$(AS) $(ASFLAGS) -D FORCE_THREADS=1 $< -o $@

$(SERIAL_BIN): $(SERIAL_OBJ)
	ld $(LDFLAGS) -o $@ $<

# Test hooks: getdents reports DT_UNKNOWN for every entry and the wide prepass
# is forced, so walk_wide's lstat fallback is exercised.
$(UNKNOWN_BUILD_DIR):
	mkdir -p $@

$(UNKNOWN_OBJ): $(SRC) | $(UNKNOWN_BUILD_DIR)
	$(AS) $(ASFLAGS) -D FORCE_UNKNOWN_TYPE -D FORCE_WIDE_SPLIT $< -o $@

$(UNKNOWN_BIN): $(UNKNOWN_OBJ)
	ld $(LDFLAGS) -o $@ $<

# Test hooks: every entry is DT_UNKNOWN and the range thresholds are lowered,
# so a 16-entry directory takes walk_range and small ones take walk_dir's
# inline loop; their lstat fallbacks are exercised. FORCE_UNKNOWN_TYPE hides
# the directory count, so walk_wide is not taken here: see the unknown build.
$(RANGE_BUILD_DIR):
	mkdir -p $@

$(RANGE_OBJ): $(SRC) | $(RANGE_BUILD_DIR)
	$(AS) $(ASFLAGS) -D FORCE_UNKNOWN_TYPE -D RANGE_MIN=16 -D CHUNK_MIN=4 $< -o $@

$(RANGE_BIN): $(RANGE_OBJ)
	ld $(LDFLAGS) -o $@ $<

run: $(BIN)
	./$(BIN)

clean:
	rm -rf build

PRESET ?= default
ZFIND_TESTDATA ?= /tmp/zfind-testdata
testdata:
	ZFIND_TESTDATA=$(ZFIND_TESTDATA) python3 tests/mktrees.py --preset $(PRESET)

# Extra pytest arguments; test-quiet sets them.
PYTEST_ARGS ?=

# Build parallel, serial and DT_UNKNOWN-hook binaries, then compare them in
# the test suite.
test: $(BIN) $(SERIAL_BIN) $(UNKNOWN_BIN) $(RANGE_BIN)
	ZFIND=$(abspath $(BIN)) ZFIND_SERIAL=$(abspath $(SERIAL_BIN)) \
	ZFIND_UNKNOWN=$(abspath $(UNKNOWN_BIN)) \
	ZFIND_RANGE=$(abspath $(RANGE_BIN)) pytest $(PYTEST_ARGS)

# Quiet run for agents: only failures and a one-line summary. pytest.ini sets
# -vv; `-o addopts=` clears it, so the strict flags are repeated here. -s also
# hides the build and recipe echo.
test-quiet:
	@$(MAKE) --no-print-directory -s test PYTEST_ARGS='-o addopts= -q --no-header --tb=short -rfE --strict-markers --strict-config'
