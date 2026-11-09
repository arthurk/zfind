{
  description = "x86-64 Assembly Development Environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          nasm        # Assembler
          binutils    # Linker (ld), objdump, readelf
          gdb         # Debugger
          strace      # syscall fault injection in tests
          xxd         # hex + ASCII dump
          gnumake     # Makefile targets
          python314
          python314Packages.pytest
          hyperfine   # `make bench` timing runner
          git         # `git describe` in bench.py and cqa.py
        ];

        shellHook = "ulimit -c 0";   # debug live under gdb, don't litter core dumps
      };
    };
}
