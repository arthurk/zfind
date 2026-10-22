ARCH ?= x86_64

BUILD_DIR := build/$(ARCH)
BIN       := $(BUILD_DIR)/zfind
OBJ       := $(BUILD_DIR)/zfind.o
SRC       := src/$(ARCH)/zfind.asm

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
.PHONY: all run clean testdata

all: $(BIN)

$(BUILD_DIR):
	mkdir -p $@

$(OBJ): $(SRC) | $(BUILD_DIR)
	$(AS) $(ASFLAGS) $< -o $@

$(BIN): $(OBJ)
	ld $(LDFLAGS) -o $@ $<

run: $(BIN)
	./$(BIN)

clean:
	rm -rf build

PRESET ?= default
testdata:
	python3 tools/mktrees.py --preset $(PRESET)
