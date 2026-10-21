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
          xxd         # hex + ASCII dump
          gnumake     # Makefile targets
          python3     # testdata generator (tools/mktrees.py)
        ];

        shellHook = "ulimit -c 0";   # debug live under gdb, don't litter core dumps
      };
    };
}
