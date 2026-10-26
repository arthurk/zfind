ARCH ?= x86_64

BUILD_DIR := build/$(ARCH)
BIN       := $(BUILD_DIR)/zfind
OBJ       := $(BUILD_DIR)/zfind.o
SRC       := src/$(ARCH)/zfind.asm
SERIAL_BUILD_DIR ?= $(BUILD_DIR)-serial
SERIAL_BIN := $(SERIAL_BUILD_DIR)/zfind
SERIAL_OBJ := $(SERIAL_BUILD_DIR)/zfind.o

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
.PHONY: all run clean testdata test

all: $(BIN)

$(BUILD_DIR):
	mkdir -p $@

$(OBJ): $(SRC) | $(BUILD_DIR)
	$(AS) $(ASFLAGS) $< -o $@

$(BIN): $(OBJ)
	ld $(LDFLAGS) -o $@ $<

$(SERIAL_BUILD_DIR):
	mkdir -p $@

$(SERIAL_OBJ): $(SRC) | $(SERIAL_BUILD_DIR)
	$(AS) $(ASFLAGS) -D FORCE_THREADS=1 $< -o $@

$(SERIAL_BIN): $(SERIAL_OBJ)
	ld $(LDFLAGS) -o $@ $<

run: $(BIN)
	./$(BIN)

clean:
	rm -rf build

PRESET ?= default
testdata:
	python3 tests/mktrees.py --preset $(PRESET)

# Build parallel and serial binaries, then compare them in the test suite.
test: $(BIN) $(SERIAL_BIN)
	ZFIND=$(abspath $(BIN)) ZFIND_SERIAL=$(abspath $(SERIAL_BIN)) pytest
