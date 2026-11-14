# Reproduction of https://tracker.ceph.com/issues/83176:
# OSDs of a dead host are marked down minutes after their heartbeat grace.
#
# Uses only upstream NixOS 26.05 (Ceph 20.2.4), pinned below.
#
#     nix-build -A repro-stock    # runs the repro against stock Ceph
#     nix-build -A repro-fixed    # same, with the fix applied to `ceph-mon`
#     nix-build -A check-fixed    # fails if, with the fix, any OSD is marked down > 5 s after its grace
#
# Each repro build runs `runs` local clusters one after another
# (3 mons + 180 memstore OSDs each, about 30 GB RAM, about 7 minutes per run).
{
  runs ? 6,
  maxExcessSeconds ? 5,
}:
let
  nixpkgsRev = "c56a87d8e41ce69bd2af57eb35dcc117a9dfca33"; # `nixos-26.05` on 2026-11-05
  nixpkgs = builtins.fetchTarball {
    url = "https://github.com/NixOS/nixpkgs/archive/${nixpkgsRev}.tar.gz";
    sha256 = "06g8y8y9bysnpxrl6bfqmn63l8w2lx2yhj0618r34j5jfqy5vjpx";
  };
  pkgs = import nixpkgs {
    config = { };
    overlays = [ ];
  };

  ceph-stock = pkgs.ceph;

  # See `pkgs/by-name/ce/ceph/README.md` in nixpkgs for patching via `overrideScope`.
  ceph-fixed = builtins.getAttr "ceph" (
    pkgs.ceph.overrideScope (
      final: prev: {
        ceph-src = prev.ceph-src.overrideAttrs (old: {
          patches = (old.patches or [ ]) ++ [
            ./0001-mon-OSDMonitor-Prevent-late-reporters-from-resetting.patch
          ];
        });
      }
    )
  );

  mkRepro =
    name: ceph:
    pkgs.runCommand "ceph-dead-host-markdown-repro-${name}"
      {
        nativeBuildInputs = [ pkgs.python3 ];
        passthru = { inherit ceph; };
      }
      ''
        export HOME="$TMPDIR"
        mkdir -p "$out"
        for r in $(seq 1 ${toString runs}); do
          python3 ${./repro.py} \
            --ceph-bin ${ceph}/bin \
            --run-dir "$TMPDIR/r" \
            --hosts 18 --osds-per-host 10 --pgs 2048 --settle-seconds 60 \
            --prime-laggy \
            --result-json "$out/run-$r.json" \
            > "$out/run-$r.log" 2>&1 || { cat "$out/run-$r.log"; exit 1; }
          cp "$TMPDIR/r/log/cluster.log" "$out/run-$r-cluster.log"
          rm -rf "$TMPDIR/r"
          tail -n 1 "$out/run-$r.log"
        done
        python3 ${./check.py} "$out" | tee "$out/summary.txt"
      '';

  repro-stock = mkRepro "stock" ceph-stock;
  repro-fixed = mkRepro "fixed" ceph-fixed;
in
{
  inherit
    ceph-stock
    ceph-fixed
    repro-stock
    repro-fixed
    ;

  check-fixed = pkgs.runCommand "ceph-dead-host-markdown-check-fixed" { nativeBuildInputs = [ pkgs.python3 ]; } ''
    python3 ${./check.py} --max-excess ${toString maxExcessSeconds} ${repro-fixed}
    touch "$out"
  '';
}
