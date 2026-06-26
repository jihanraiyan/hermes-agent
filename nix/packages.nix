# nix/packages.nix — Hermes Agent package built with uv2nix
{ inputs, ... }:
{
  perSystem =
    {
      pkgs,
      lib,
      ...
    }:
    let
      hermesNpmLib = pkgs.callPackage ./lib.nix { };
      hermesAgent = pkgs.callPackage ./hermes-agent.nix {
        inherit (inputs)
          uv2nix
          pyproject-nix
          pyproject-build-systems
          ;
        inherit
          hermesNpmLib
          ;

        # Only embed clean revs. dirtyRev doesn't represent any upstream
        # commit, so comparing it would always claim "update available".
        rev = inputs.self.rev or null;
      };

      desktop = pkgs.callPackage ./desktop.nix {
        inherit hermesAgent hermesNpmLib;
      };
      desktop-thin = pkgs.callPackage ./desktop.nix {
        inherit hermesNpmLib;
      };
    in
    {
      packages = {
        default = hermesAgent;

        # Ships discord.py + python-telegram-bot + slack-sdk so a plain
        # `nix profile install .#messaging` connects to Discord/Telegram/Slack
        # on first run — lazy-install can't write to the read-only /nix/store.
        messaging = hermesAgent.override {
          extraDependencyGroups = [ "messaging" ];
        };

        # All platform-portable optional integrations pre-built.
        # matrix is Linux-only (oqs/liboqs lacks aarch64-darwin wheels).
        full = hermesAgent.override {
          extraDependencyGroups = [
            "anthropic"
            "azure-identity"
            "bedrock"
            "daytona"
            "dingtalk"
            "edge-tts"
            "exa"
            "fal"
            "feishu"
            "firecrawl"
            "hindsight"
            "honcho"
            "messaging"
            "modal"
            "parallel-web"
            "tts-premium"
            "voice"
          ]
          ++ lib.optionals pkgs.stdenv.isLinux [ "matrix" ];
        };

        inherit desktop desktop-thin;
      };
    };
}
