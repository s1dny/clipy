{
  description = "clipy, built, and the NixOS module that runs it";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    crane.url = "github:ipetkov/crane/v0.24.0";
    rust-overlay = {
      url = "github:oxalica/rust-overlay";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      crane,
      rust-overlay,
    }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        overlays = [ rust-overlay.overlays.default ];
        # Claude Code, which the agent runs.
        config.allowUnfree = true;
      };
      inherit (pkgs) lib;
      craneLib = (crane.mkLib pkgs).overrideToolchain pkgs.rust-bin.stable."1.99.1".default;
      args = {
        pname = "clipy";
        version = "0.1.0";
        src = lib.fileset.toSource {
          root = ./.;
          fileset = lib.fileset.unions [
            ./Cargo.toml
            ./Cargo.lock
            ./src
            ./assets
            ./tests
          ];
        };
        strictDeps = true;
        doCheck = false;
        cargoExtraArgs = "--locked";
        nativeBuildInputs = [
          pkgs.cmake
          pkgs.perl
        ];
        CARGO_BUILD_RUSTFLAGS = "-C target-cpu=x86-64-v3";
      };
      # What clipy runs: ffmpeg, yt-dlp (with Deno for YouTube's challenges) and Claude Code.
      tools = with pkgs; [
        ffmpeg-headless
        yt-dlp
        deno
        claude-code
        curl
        jq
      ];
      # The binary, run in the share directory that holds the agent's prompts, which it
      # reads from its working directory.
      clipy = craneLib.buildPackage (
        args
        // {
          cargoArtifacts = craneLib.buildDepsOnly args;
          nativeBuildInputs = args.nativeBuildInputs ++ [ pkgs.makeWrapper ];
          postInstall = ''
            mkdir -p $out/share/clipy
            cp -R ${./agent} $out/share/clipy/agent
            chmod -R u+w $out/share/clipy
            wrapProgram $out/bin/clipy --prefix PATH : ${lib.makeBinPath tools}
          '';
          meta.mainProgram = "clipy";
        }
      );
    in
    {
      packages.${system} = {
        inherit clipy;
        default = clipy;
      };

      nixosModules.default =
        { lib, pkgs, ... }:
        {
          imports = [ ./nix/module.nix ];
          services.clipy.package = lib.mkDefault self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        };
    };
}
