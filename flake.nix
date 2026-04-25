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
          playwright-driver.browsers
        ];

        shellHook = ''
          unset SOURCE_DATE_EPOCH
          export PODMAN_USERNS=keep-id
          export LD_LIBRARY_PATH=${pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ]}:$LD_LIBRARY_PATH
          export PLAYWRIGHT_BROWSERS_PATH=${pkgs.playwright-driver.browsers}
          export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=true
          if [ ! -d .venv ]; then
            python -m venv .venv
          fi
          source .venv/bin/activate
        '';
      };
    };
}
