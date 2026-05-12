#!/usr/bin/env python3
"""
Deploy a QEMU/KVM VM from an ISO using virt-install, virtio devices, and optional SR-IOV VF.
Guest CPU architecture matches the host (uname -m).

Install media: pass --cdrom to virt-install (required so it recognizes the install
method). The installer inserts the CD device before other disks so boot=cdrom,hd
prefers the ISO over an empty virtio disk.

For qemu:///session there is no libvirt "default" NAT network; the script switches to
user (slirp) networking unless you pass --network explicitly. List those VMs with the
same URI (virsh --connect qemu:///session ...), not qemu:///system.

After the guest OS is installed and running, you can print its IPv4 with:
  python3 deploy_vm.py --print-ip --name my-vm
Do not pass --iso for --print-ip (it is ignored). Use the same --connect URI as for
virt-install. Requires virsh; qemu-guest-agent in the guest improves reliability.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional


def run(
    cmd: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=check,
    )


def host_arch() -> str:
    p = subprocess.run(["uname", "-m"], capture_output=True, text=True, check=True)
    return p.stdout.strip()


def require_cmd(name: str) -> None:
    p = subprocess.run(["bash", "-lc", f"command -v {name}"], capture_output=True)
    if p.returncode != 0:
        print(f"Required executable not found: {name}", file=sys.stderr)
        sys.exit(1)


def qemu_img_create(path: Path, size_gb: int = 32) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        print(f"Disk already exists: {path}", file=sys.stderr)
        return
    run(
        [
            "qemu-img",
            "create",
            "-f",
            "qcow2",
            str(path),
            f"{size_gb}G",
        ],
    )


def pci_to_virt_install_id(pci_bdf: str) -> str:
    """Convert 0000:ab:cd.e -> pci_0000_ab_cd_e for virt-install --hostdev."""
    s = pci_bdf.strip().lower()
    if not s.startswith("0000:"):
        s = "0000:" + s
    # 0000:bb:dd.f
    tail = s[5:]
    bus, rest = tail.split(":", 1)
    slot, func = rest.split(".", 1)
    return f"pci_0000_{bus}_{slot}_{func}"


def first_vf_pci_for_pf(pf_iface: str) -> Optional[str]:
    """Return PCI BDF (0000:aa:bb.c) of the first SR-IOV VF under PF net device."""
    pf_dev = Path(f"/sys/class/net/{pf_iface}/device").resolve()
    if not pf_dev.exists():
        raise FileNotFoundError(f"Network interface {pf_iface!r} not found")
    for child in sorted(pf_dev.glob("virtfn*")):
        vf = child.resolve()
        name = vf.name
        if name.startswith("0000:"):
            return name
    return None


def _parse_ipv4_from_domifaddr_text(text: str) -> Optional[str]:
    for line in text.splitlines():
        m = re.search(
            r"\bipv4\b\s+(\d{1,3}(?:\.\d{1,3}){3})(?:/\d+)?",
            line,
            re.IGNORECASE,
        )
        if m:
            ip = m.group(1)
            if not ip.startswith(("127.", "0.")):
                return ip
    return None


def _domiflist_network_macs(domain: str, connect: str) -> list[tuple[str, str]]:
    """Pairs (libvirt_network_name, mac) for interfaces attached to a libvirt network."""
    p = subprocess.run(
        ["virsh", "-c", connect, "domiflist", domain],
        capture_output=True,
        text=True,
        check=False,
    )
    if p.returncode != 0 or not p.stdout:
        return []
    out: list[tuple[str, str]] = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("-") or line.lower().startswith("interface"):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        if parts[1].lower() != "network":
            continue
        net_name = parts[2]
        mac = parts[-1]
        if re.match(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$", mac):
            out.append((net_name, mac.lower()))
    return out


def _normalize_mac(s: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", s).lower()


def _ipv4_from_net_dhcp_leases(connect: str, macs: list[tuple[str, str]]) -> Optional[str]:
    """Match guest MAC against virsh net-dhcp-leases <network> output."""
    want = {_normalize_mac(m) for _, m in macs}
    if not want:
        return None
    nets = sorted({n for n, _ in macs})
    mac_re = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
    for net in nets:
        p = subprocess.run(
            ["virsh", "-c", connect, "net-dhcp-leases", net],
            capture_output=True,
            text=True,
            check=False,
        )
        if p.returncode != 0 or not p.stdout:
            continue
        for line in p.stdout.splitlines():
            for mac_found in mac_re.findall(line):
                if _normalize_mac(mac_found) not in want:
                    continue
                m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})/\d+", line)
                if m:
                    ip = m.group(1)
                    if not ip.startswith(("127.", "0.")):
                        return ip
                break
    return None


def guest_ipv4_from_virsh(domain: str, connect: str) -> Optional[str]:
    """
    Try to read the guest's IPv4 via libvirt: domifaddr (auto + agent/lease/arp), then
    net-dhcp-leases on the libvirt network(s) attached to the domain.

    Requires virsh. The guest must be running; for NAT networks, DHCP lease or guest
    agent must expose the address.
    """
    # Let libvirt choose the best source (some versions behave better without --source).
    p0 = subprocess.run(
        ["virsh", "-c", connect, "domifaddr", domain],
        capture_output=True,
        text=True,
        check=False,
    )
    if p0.returncode == 0 and p0.stdout:
        ip = _parse_ipv4_from_domifaddr_text(p0.stdout)
        if ip:
            return ip

    for source in ("agent", "lease", "arp"):
        p = subprocess.run(
            ["virsh", "-c", connect, "domifaddr", domain, "--source", source],
            capture_output=True,
            text=True,
            check=False,
        )
        if p.returncode != 0 or not p.stdout:
            continue
        ip = _parse_ipv4_from_domifaddr_text(p.stdout)
        if ip:
            return ip

    macs = _domiflist_network_macs(domain, connect)
    return _ipv4_from_net_dhcp_leases(connect, macs)


def _is_session_uri(connect: str) -> bool:
    return "session" in (connect or "").strip().lower()


def parse_network_args(spec: str) -> list[str]:
    """
    default          -> virtio NIC on libvirt network "default" (qemu:///system only)
    user             -> QEMU user networking (slirp); use with qemu:///session
    bridge:br0       -> virtio bridge attachment
    sriov:eth0       -> PCI passthrough of first VF under PF (SR-IOV)
    """
    if spec == "default":
        return ["--network", "network=default,model=virtio"]
    if spec == "user":
        return ["--network", "user,model=virtio"]
    if spec.startswith("bridge:"):
        br = spec.split(":", 1)[1]
        if not br:
            print("bridge: needs an interface name, e.g. bridge:br0", file=sys.stderr)
            sys.exit(1)
        return ["--network", f"bridge={br},model=virtio"]
    if spec.startswith("sriov:"):
        pf = spec.split(":", 1)[1]
        vf = first_vf_pci_for_pf(pf)
        if not vf:
            print(
                f"No SR-IOV VF under {pf!r}. Enable VFs (e.g. sriov_numvfs) or "
                "use --network user (session) or bridge:INTERFACE.",
                file=sys.stderr,
            )
            sys.exit(1)
        hostdev = pci_to_virt_install_id(vf)
        return ["--hostdev", hostdev]
    print(
        f"Invalid --network {spec!r}. Use: default | user | bridge:BR | sriov:PF_IFACE",
        file=sys.stderr,
    )
    sys.exit(1)


def build_virt_install_cmd(args: argparse.Namespace, disk_path: Path) -> list[str]:
    net_args = parse_network_args(args.network)
    cmd: list[str] = [
        "virt-install",
        "--connect",
        args.connect,
        "--name",
        args.name,
        "--memory",
        str(args.memory),
        "--vcpus",
        str(args.vcpus),
        # --cdrom is required: virt-install only treats ISO as an install method here,
        # not when the same path is given only as --disk device=cdrom.
        "--cdrom",
        args.iso,
        "--disk",
        f"path={disk_path},bus=virtio,cache=none",
        "--boot",
        "cdrom,hd",
        "--graphics",
        args.graphics,
        "--console",
        "pty,target_type=virtio",
    ]
    if not args.autoconsole:
        cmd.append("--noautoconsole")
    cmd.extend(
        [
            "--os-variant",
            args.os_variant,
            "--virt-type",
            "kvm",
            "--video",
            "virtio",
            "--controller",
            "type=virtio-serial",
            "--rng",
            "/dev/urandom,model=virtio",
            "--sound",
            "none",
            "--watchdog",
            "default",
        ]
    )
    if args.arch:
        cmd.extend(["--arch", args.arch])
    cmd.extend(net_args)
    if args.extra_args:
        cmd.extend(args.extra_args)
    return cmd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--iso",
        default="",
        help="Path to installation ISO (not used with --print-ip)",
    )
    parser.add_argument("--name", required=True, help="libvirt domain name")
    parser.add_argument("--vcpus", type=int, default=2)
    parser.add_argument("--memory", type=int, default=4096, help="RAM in MiB")
    parser.add_argument(
        "--network",
        default="default",
        help=(
            "default (NAT libvirt network; qemu:///system) | user (slirp; for qemu:///session) "
            "| bridge:br0 | sriov:PF_IFACE"
        ),
    )
    parser.add_argument(
        "--disk",
        default="",
        help=(
            "qcow2 path (default: /var/lib/libvirt/images/<name>.qcow2, root-writable only). "
            "As a normal user use e.g. --disk $HOME/libvirt-images/<name>.qcow2"
        ),
    )
    parser.add_argument(
        "--disk-size-gb",
        type=int,
        default=32,
        help="Only used when creating a new qcow2",
    )
    parser.add_argument(
        "--connect",
        default=os.environ.get("LIBVIRT_DEFAULT_URI", "qemu:///system"),
    )
    parser.add_argument(
        "--os-variant",
        default="generic",
        help='virt-install --os-variant (try "detect=on" where supported)',
    )
    parser.add_argument(
        "--arch",
        default="",
        help="Guest arch (default: from uname -m)",
    )
    parser.add_argument(
        "--skip-disk-create",
        action="store_true",
        help="Do not run qemu-img create",
    )
    parser.add_argument(
        "--graphics",
        default="none",
        help="Guest display backend: none | spice | vnc (default: none).",
    )
    parser.add_argument(
        "--autoconsole",
        action="store_true",
        help="Open console window automatically during install (requires virt-viewer/spice).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print virt-install invocation only",
    )
    parser.add_argument(
        "--wait",
        default="-1",
        help="virt-install --wait (default -1: until shutdown)",
    )
    parser.add_argument(
        "--print-ip",
        action="store_true",
        help="Print guest IPv4 for --name and exit (virsh domifaddr; install qemu-guest-agent on guest for best results)",
    )
    parser.add_argument(
        "extra_args",
        nargs="*",
        help="Additional arguments forwarded to virt-install",
    )
    args = parser.parse_args()
    args.extra_args = list(args.extra_args)

    if args.print_ip:
        require_cmd("virsh")
        if args.iso:
            print(
                "Note: --iso is not used with --print-ip (only --name and --connect matter).",
                file=sys.stderr,
            )
        ip = guest_ipv4_from_virsh(args.name, args.connect)
        if not ip:
            print(
                f"Could not determine IPv4 for domain {args.name!r}.\n"
                "  - Is the VM running?  virsh list --state-running\n"
                "  - Try manually:  virsh domifaddr {0!r}\n"
                "  - NAT + DHCP: ensure the guest requests DHCP on the virtio NIC.\n"
                "  - Guest agent: install qemu-guest-agent, then:\n"
                "      virsh domifaddr {0!r} --source agent\n"
                "  - Or inspect leases:  virsh net-dhcp-leases default".format(args.name),
                file=sys.stderr,
            )
            sys.exit(1)
        print(ip)
        return

    if not args.iso:
        print("--iso is required unless using --print-ip", file=sys.stderr)
        sys.exit(2)

    if args.network == "default" and _is_session_uri(args.connect):
        print(
            "Note: libvirt session URI has no network named 'default'. "
            "Using --network user (QEMU user/slirp). For NAT+DHCP like system libvirt, "
            "use --connect qemu:///system (often with sudo) and keep --network default.\n"
            f"Note: VMs on {args.connect!r} are listed only with the same URI, e.g.\n"
            f"  virsh --connect {args.connect} list --all",
            file=sys.stderr,
        )
        args.network = "user"

    if args.autoconsole and not os.environ.get("DISPLAY"):
        print(
            "Warning: --autoconsole needs a GUI (DISPLAY is unset); virt-viewer will not start. "
            "Use `ssh -X` from a machine with an X server, run from a local desktop session, "
            "or use headless graphics, e.g.  --graphics vnc,listen=127.0.0.1  (then connect with a VNC client).",
            file=sys.stderr,
        )

    require_cmd("virt-install")
    require_cmd("qemu-img")

    if not args.arch:
        args.arch = host_arch()

    disk_path = (
        Path(args.disk)
        if args.disk
        else Path("/var/lib/libvirt/images") / f"{args.name}.qcow2"
    )

    if not args.skip_disk_create:
        try:
            qemu_img_create(disk_path, size_gb=args.disk_size_gb)
        except subprocess.CalledProcessError as e:
            err = (e.stderr or "") + (e.stdout or "")
            print(err, file=sys.stderr)
            if "Permission denied" in err or "permission denied" in err:
                print(
                    "\nThe default image path is under /var/lib/libvirt/images/ and is "
                    "usually not writable as a normal user. Fix one of:\n"
                    "  1) Use a path you own, e.g. create a directory and pass:\n"
                    f"       mkdir -p \"$HOME/libvirt-images\" && \\\n"
                    f"       {sys.argv[0]} ... --disk \"$HOME/libvirt-images/{args.name}.qcow2\"\n"
                    "     (For qemu:///system, libvirt/qemu must be able to read that path and "
                    "file; often chmod o+rx on parent dirs, or use a group libvirt-qemu can read.)\n"
                    "  2) Run the whole deploy under sudo only if that matches your security policy.\n",
                    file=sys.stderr,
                )
            sys.exit(e.returncode)

    cmd = build_virt_install_cmd(args, disk_path)
    cmd.extend(["--wait", str(args.wait)])

    if args.dry_run:
        print(" ".join(cmd))
        return

    print("Running:", " ".join(cmd), file=sys.stderr)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
