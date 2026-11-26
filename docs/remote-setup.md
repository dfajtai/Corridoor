# Remote computer setup (Ubuntu)

Do this first, while you can still reach the machine directly or over an existing
connection. Requirements: Ubuntu, 64-bit Intel/AMD, `openssh-server` and, for
HopToDesk mode, HopToDesk **1.46.90 or newer** (1.44.4 does not work; keep **both**
computers up to date).

## 1. SSH server

```bash
sudo apt update
sudo apt install openssh-server
sudo systemctl enable --now ssh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub   # note the fingerprint
ip -4 -brief address                               # note the internal IP address
```

## 2. HopToDesk

```bash
mkdir -p ~/hoptodesk-install && cd ~/hoptodesk-install
wget -O hoptodesk.deb https://dl.hoptodesk.com/hoptodesk.deb
sudo apt install ./hoptodesk.deb
sudo systemctl enable --now hoptodesk
dpkg-query -W hoptodesk                            # 1.46.90 or newer
```

If `apt` reports unmet dependencies of an unrelated package, repair that first. Run
`sudo apt-get -s ...` to preview what a repair would remove.

In the HopToDesk window on this computer:

1. Set your own permanent password and enable unattended access.
2. Open **Settings > Network** and enable **Direct IP access** (default TCP `21118`).
3. On Wayland, enable **Settings > Security > Unattended Wayland Access**.
   If the login screen is still unreachable, switch GDM to X11: set
   `WaylandEnable=false` under `[daemon]` in `/etc/gdm3/custom.conf`, then reboot.

After an upgrade, restart it with `sudo systemctl restart hoptodesk` and check that
Direct IP access is still enabled.

## 3. Firewall

Needed only if `sudo ufw status` shows `active`. Log in the way you will use the tool
and run `echo $SSH_CLIENT`; the first value is the address this computer sees.

```bash
sudo ufw allow from <address-or-vpn-subnet> to any port 22 proto tcp
sudo ufw allow from <address-or-vpn-subnet> to any port 21118 proto tcp
sudo ufw status numbered
```

Add the SSH rule before enabling or tightening the firewall, so that you do not lock
yourself out.

## 4. Check

```bash
systemctl is-active ssh hoptodesk
ss -ltn | grep -E ':(22|21118) '
```
