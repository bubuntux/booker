{
  description = "booker dev shell";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in {
      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          podman
          python3
          python3Packages.pip
          python3Packages.virtualenv
        ];

        shellHook = ''
          export PODMAN_USERNS=keep-id
          if [ ! -d .venv ]; then
            python -m venv .venv
          fi
          source .venv/bin/activate
        '';
      };
    };
}
