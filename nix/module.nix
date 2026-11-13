# clipy as systemd units, all as the clipy user with its state in /var/lib/clipy:
#
# - clipy: the server (`clipy serve`), with the channels' MCP endpoints.
# - clipy-site: the public site, from the package's own files.
# - clipy-studio: the collector, which reads YouTube Studio through a signed-in browser's
#   DevTools port (studioCdp).
# - clipy-agent: the daily Claude sessions for the channels, then the report, on a timer.
{
  config,
  lib,
  ...
}:
let
  cfg = config.services.clipy;
  share = "${cfg.package}/share/clipy";
  base = {
    CLIPY_CONFIG = "${cfg.configFile}";
    CLIPY_DATA_DIR = "/var/lib/clipy";
    CLIPY_SERVER = "http://127.0.0.1:${toString cfg.port}";
    HOME = "/tmp";
    DISABLE_AUTOUPDATER = "1";
  }
  // cfg.environment;
  hardening = {
    User = "clipy";
    Group = "clipy";
    WorkingDirectory = share;
    EnvironmentFile = cfg.environmentFiles;
    ReadWritePaths = [ "/var/lib/clipy" ];
    PrivateTmp = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    PrivateDevices = true;
    NoNewPrivileges = true;
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectControlGroups = true;
    LockPersonality = true;
    RestrictSUIDSGID = true;
    RestrictAddressFamilies = [
      "AF_UNIX"
      "AF_INET"
      "AF_INET6"
    ];
    UMask = "0027";
    Slice = cfg.slice;
  };
  daemon = description: command: environment: memory: {
    inherit description;
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    wantedBy = [ "multi-user.target" ];
    environment = base // environment;
    serviceConfig = hardening // {
      ExecStart = "${lib.getExe cfg.package} ${command}";
      MemoryMax = memory;
      Restart = "always";
      RestartSec = "5s";
    };
  };
in
{
  options.services.clipy = {
    enable = lib.mkEnableOption "clipy";
    package = lib.mkOption { type = lib.types.package; };
    configFile = lib.mkOption {
      type = lib.types.path;
      description = "clipy's config.toml: its channels and their settings.";
    };
    port = lib.mkOption {
      type = lib.types.port;
      default = 3080;
    };
    sitePort = lib.mkOption {
      type = lib.types.port;
      default = 3081;
    };
    studioCdp = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "http://127.0.0.1:9222";
      description = "The signed-in browser's DevTools; without it the collector does not run.";
    };
    studioSession = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/clipy/studio-session/google-cookies.json";
    };
    agentChannels = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "The channels the daily agent runs for; none, and it does not run.";
    };
    agentAt = lib.mkOption {
      type = lib.types.str;
      default = "*-*-* 04:00:00";
    };
    environment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
    };
    environmentFiles = lib.mkOption {
      type = lib.types.listOf lib.types.path;
      default = [ ];
    };
    slice = lib.mkOption {
      type = lib.types.str;
      default = "system.slice";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.clipy = {
      isSystemUser = true;
      group = "clipy";
      home = "/var/lib/clipy";
    };
    users.groups.clipy = { };
    systemd.tmpfiles.rules = [ "d /var/lib/clipy 0750 clipy clipy -" ];

    systemd.services = {
      clipy = daemon "clipy" "serve" {
        HOST = "127.0.0.1";
        PORT = toString cfg.port;
      } "4G";
      clipy-site =
        daemon "clipy's site" "site --dir ${share}/site --port ${toString cfg.sitePort}" { }
          "128M";
    }
    // lib.optionalAttrs (cfg.studioCdp != null) {
      clipy-studio = daemon "clipy's YouTube Studio collector" "studio" {
        CLIPY_STUDIO_CDP = cfg.studioCdp;
        CLIPY_STUDIO_SESSION = cfg.studioSession;
      } "512M";
    }
    // lib.optionalAttrs (cfg.agentChannels != [ ]) {
      clipy-agent = {
        description = "clipy's daily agent sessions";
        after = [
          "network-online.target"
          "clipy.service"
        ];
        wants = [ "network-online.target" ];
        environment = base;
        startAt = cfg.agentAt;
        serviceConfig = hardening // {
          Type = "oneshot";
          ExecStart = "${lib.getExe cfg.package} agent ${lib.concatStringsSep " " cfg.agentChannels}";
          MemoryMax = "2G";
          TimeoutStartSec = "6h";
        };
      };
    };
  };
}
