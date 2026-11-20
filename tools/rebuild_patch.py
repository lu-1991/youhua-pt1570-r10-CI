#!/usr/bin/env python3
"""Rebuild the PT1570-R10 adaptation on top of a clean PonWRT-TYFZA1 checkout.

Run from the repository root of the upstream clone; nothing is committed.
The result is `git diff` -> patches/oem/0001-oem-uboot-image-layout.patch.
（只用于 oem 目标；fit 目标不改上游源码，见 patches/fit/README.md）
"""
import pathlib
import sys

MIGRATE = "port/packages/tyfza1-storage/files/tyfza1-migrate-storage"
STORAGE = "port/overlay/target/linux/rtl9617c/base-files/lib/upgrade/tyfza1-storage.sh"
PLATFORM = "port/overlay/target/linux/rtl9617c/base-files/lib/upgrade/platform.sh"


def edit(path, pairs):
    p = pathlib.Path(path)
    text = p.read_text(encoding="utf-8")
    for old, new in pairs:
        n = text.count(old)
        if n != 1:
            sys.exit("%s: anchor matched %d times: %r" % (path, n, old[:80]))
        text = text.replace(old, new)
    p.write_text(text, encoding="utf-8")
    print("patched", path)


# ---------------------------------------------------------------- migrate
edit(MIGRATE, [
(
"""import_hashes() (cd "$1" && find . -type f -exec sha256sum {} \\; | sort)""",
"""# PT1570-R10 keeps the same OEM volume names but assigns different IDs (it has
# no ubi_fpga and carries an extra ubi_dpi), so every OEM volume is located by
# name. A missing name is not an error: one list then serves both layouts.
vol_id() {
	local p
	for p in "$sys"/ubi0_*/name; do
		[ -f "$p" ] || continue
		[ "$(cat "$p")" = "$1" ] || continue
		p=${p%/name}
		echo "${p##*_}"
		return 0
	done
	return 1
}
remove_name() { local id; id=$(vol_id "$1") || return 0; ubirmvol "$dev" -n "$id"; }
import_hashes() (cd "$1" && find . -type f -exec sha256sum {} \\; | sort)"""
),
(
"""other=$((1 - slot))
software="1:ubi_fpga 2:ubi_DTB0 5:ubi_DTB1 8:ubi_framework1 9:ubi_framework2"
discard="$software $((3 + other * 3)):ubi_k$other $((4 + other * 3)):ubi_r$other 0:ubi_Config 10:ubi_apps"
for entry in "$((3 + slot * 3)):ubi_k$slot" "$((4 + slot * 3)):ubi_r$slot"; do
	id=${entry%%:*}
	exists "$id" && [ "$(name "$id")" = "${entry#*:}" ] || fail "Missing active $entry"
done
remaining=0
for entry in $discard 11:rootfs_data; do
	id=${entry%%:*}
	exists "$id" || continue
	[ "$(name "$id")" = "${entry#*:}" ] || fail "Unexpected volume ID$id"
	[ "$id" = 11 ] || remaining=$((remaining + 1))
done""",
"""other=$((1 - slot))
# OEM software volumes. ubi_fpga only exists on TYFZA1, ubi_dpi only on PT1570.
software="ubi_fpga ubi_DTB0 ubi_DTB1 ubi_framework1 ubi_framework2"
discard="$software ubi_k$other ubi_r$other ubi_Config ubi_apps ubi_dpi"
for vol in "ubi_k$slot" "ubi_r$slot"; do
	vol_id "$vol" >/dev/null || fail "Missing active $vol"
done
remaining=0
for vol in $discard; do
	vol_id "$vol" >/dev/null || continue
	remaining=$((remaining + 1))
done"""
),
(
"""for entry in $discard 11:rootfs_data; do
	id=${entry%%:*}
	grep -Eq "^(/dev/ubi0_$id|ubi0:${entry#*:}) " /proc/mounts && fail "Volume ID$id is mounted"
done""",
"""for vol in $discard rootfs_data; do
	id=$(vol_id "$vol") || continue
	grep -Eq "^(/dev/ubi0_$id|ubi0:$vol) " /proc/mounts && fail "$vol is mounted"
done"""
),
(
"""grow_data() {
	local free target magic
	exists 11 || fail 'Missing data volume'
	[ "$(cat "$sys/ubi0_11/upd_marker")" = 0 ] || fail 'Interrupted data update'
	free=$(free_blocks)
	[ "$free" -gt 0 ] || return 0
	target=$(( $(blocks 11) + free ))
	magic=$(word "${dev}_11" 0)
	case "$magic" in
		101718065) [ "$(word "${dev}_11" 44)" -ge "$target" ] || fail 'UBIFS growth limit too small';;
		4294967295) ;; # Blank volume left by sysupgrade -n / factory reset.
		*) fail 'Unknown data filesystem';;
	esac
	ubirsvol "$dev" -n 11 -s "$((target * leb))"
}""",
"""grow_data() {
	local free target magic data
	data=$(vol_id rootfs_data) || fail 'Missing data volume'
	[ "$(cat "$sys/ubi0_$data/upd_marker")" = 0 ] || fail 'Interrupted data update'
	free=$(free_blocks)
	[ "$free" -gt 0 ] || return 0
	target=$(( $(blocks "$data") + free ))
	magic=$(word "${dev}_$data" 0)
	case "$magic" in
		101718065) [ "$(word "${dev}_$data" 44)" -ge "$target" ] || fail 'UBIFS growth limit too small';;
		4294967295) ;; # Blank volume left by sysupgrade -n / factory reset.
		*) fail 'Unknown data filesystem';;
	esac
	ubirsvol "$dev" -N rootfs_data -s "$((target * leb))"
}"""
),
(
"""	exists 11 && [ "$(cat "$sys/ubi0_11/upd_marker")" = 0 ] || fail 'Completed transaction lost data volume'""",
"""	data=$(vol_id rootfs_data) && [ "$(cat "$sys/ubi0_$data/upd_marker")" = 0 ] ||
		fail 'Completed transaction lost data volume'"""
),
(
"""	if exists 11; then
		magic=$(word "${dev}_11" 0)
		if [ "$magic" = 101718065 ]; then
			mount -t ubifs -o ro,noatime "${dev}_11" "$work/mount\"""",
"""	data=$(vol_id rootfs_data) || data=
	if [ -n "$data" ]; then
		magic=$(word "${dev}_$data" 0)
		if [ "$magic" = 101718065 ]; then
			mount -t ubifs -o ro,noatime "${dev}_$data" "$work/mount\""""
),
(
"""	exists 0 && exists 10 || fail 'Missing original factory sources'""",
"""	factory_config=$(vol_id ubi_Config) || fail 'Missing original ubi_Config'
	factory_apps=$(vol_id ubi_apps) || fail 'Missing original ubi_apps'"""
),
(
"""	for entry in 0:config 10:apps; do
		id=${entry%%:*}; component=${entry#*:}
		[ "$(cat "$sys/ubi0_$id/upd_marker")" = 0 ] || fail 'Original volume has interrupted update'""",
"""	for entry in ubi_Config:config ubi_apps:apps; do
		id=$(vol_id "${entry%%:*}"); component=${entry#*:}
		[ "$(cat "$sys/ubi0_$id/upd_marker")" = 0 ] || fail 'Original volume has interrupted update'"""
),
(
"""mount -t ubifs -o ro,noatime "${dev}_0" """,
"""mount -t ubifs -o ro,noatime "${dev}_$factory_config" """
),
(
"""mount -t ubifs -o ro,noatime "${dev}_10" """,
"""mount -t ubifs -o ro,noatime "${dev}_$factory_apps" """
),
(
"""apps_bytes=$(LC_ALL=C tr -d '\\377' < "${dev}_10" | wc -c)""",
"""apps_bytes=$(LC_ALL=C tr -d '\\377' < "${dev}_$factory_apps" | wc -c)"""
),
(
"""	for entry in 0:config 10:apps; do
		id=${entry%%:*}; component=${entry#*:}
		expected=$(sed -n "s/  $component.ubifs\\$//p" "$work/capsule/raw.sha256")""",
"""	for entry in ubi_Config:config ubi_apps:apps; do
		id=$(vol_id "${entry%%:*}"); component=${entry#*:}
		expected=$(sed -n "s/  $component.ubifs\\$//p" "$work/capsule/raw.sha256")"""
),
(
"""	for entry in $software 12:tyf_factory_a_pending 13:tyf_factory_b_pending; do
		id=${entry%%:*}
		exists "$id" || continue
		available=$((available + $(blocks "$id")))
	done""",
"""	for vol in $software; do
		id=$(vol_id "$vol") || continue
		available=$((available + $(blocks "$id")))
	done
	for id in 12 13; do
		exists "$id" || continue
		available=$((available + $(blocks "$id")))
	done"""
),
(
"""	for entry in $software; do remove "${entry%%:*}"; done""",
"""	for vol in $software; do remove_name "$vol"; done"""
),
(
"""for entry in $discard; do remove "${entry%%:*}"; done
remove 11
create 11 -N rootfs_data -m""",
"""for vol in $discard; do remove_name "$vol"; done
# rootfs_data always lands on the first ID after the eleven OEM volumes.
remove_name rootfs_data
create 11 -N rootfs_data -m
[ "$(name 11)" = rootfs_data ] || fail 'rootfs_data did not take ID 11'"""
),
])

# ---------------------------------------------------------------- storage
edit(STORAGE, [
(
"""	case "$(cat "$path/name"):$rootvol" in
		ubi_r0:${ubidev}_4|ubi_r1:${ubidev}_7) ;;
		*) echo "Unexpected TYFZA1 root volume: $rootvol" >&2; return 1 ;;
	esac""",
"""	# PT1570-R10 numbers ubi_r0/ubi_r1 as 3/6 instead of 4/7, so the slot is
	# identified by volume name only.
	case "$(cat "$path/name")" in
		ubi_r0|ubi_r1) ;;
		*) echo "Unexpected TYFZA1 root volume: $rootvol ($(cat "$path/name"))" >&2; return 1 ;;
	esac"""
),
(
"""	if [ -n "$data" ]; then
		[ "$data" = "${ubidev}_11" ] || return 1
		data_ebs=$(cat "/sys/class/ubi/$data/reserved_ebs")""",
"""	if [ -n "$data" ]; then
		data_ebs=$(cat "/sys/class/ubi/$data/reserved_ebs")"""
),
(
"""		ubimkvol "/dev/$ubidev" -n 11 -N rootfs_data -m""",
"""		ubimkvol "/dev/$ubidev" -n 11 -N rootfs_data -m || return 1
		[ "$(cat "/sys/class/ubi/${ubidev}_11/name")" = rootfs_data ] || return 1"""
),
])

# --------------------------------------------------------------- platform
edit(PLATFORM, [
(
"""
platform_do_upgrade() {""",
"""
# ---------------------------------------------------------------------------
# PT1570-R10 (Youhua, 10G-EPON) runs the stock Realtek U-Boot of the TYFZA1
# family, but its loader does not understand FIT: 'process1' reads exactly
# 6 MiB from ubi_k<slot>, LZMA-decodes it in place, reads a 256 KiB device tree
# from ubi_DTB<slot> and calls 'booti'. The OEM volume names are identical to
# TYFZA1, the IDs are not (no ubi_fpga, an extra ubi_dpi, so ubi_r0/ubi_r1 are
# IDs 3/6 instead of 4/7) - hence every volume below is resolved by name.
pt1570_oem_kernel_bytes=6291456
pt1570_oem_rootfs_bytes=33648640
pt1570_oem_dtb_bytes=262144

pt1570_oem_boot() {
	grep -q 'pt1570\\.oem=1' /proc/cmdline
}

# Write the layout the stock loader reads: the kernel volume filled up to the
# 6 MiB the boot script always reads, then the SquashFS root volume. The
# overlay is left alone, so an in-system upgrade keeps the configuration.
pt1570_oem_upgrade() {
	local image="$1" slot="$2" board_dir ubidev kvol rvol dvol
	local ktmp rtmp dtmp ksize rsize dsize pad

	board_dir=$(tar tf "$image" | grep -m 1 '^sysupgrade-.*/$') || return 1
	board_dir=${board_dir%/}
	ubidev=$(nand_find_ubi "$CI_UBIPART") || return 1
	kvol=$(nand_find_volume "$ubidev" "ubi_k$slot") || return 1
	rvol=$(nand_find_volume "$ubidev" "ubi_r$slot") || return 1
	dvol=$(nand_find_volume "$ubidev" "ubi_DTB$slot")
	ktmp=/tmp/pt1570-kernel.lzma
	rtmp=/tmp/pt1570-root.squashfs
	dtmp=/tmp/pt1570-dtb.img
	rm -f "$ktmp" "$rtmp" "$dtmp"

	tar xOf "$image" "$board_dir/kernel" > "$ktmp" || return 1
	tar xOf "$image" "$board_dir/root" > "$rtmp" || return 1
	ksize=$(wc -c < "$ktmp")
	rsize=$(wc -c < "$rtmp")
	[ "$ksize" -gt 0 ] && [ "$ksize" -le "$pt1570_oem_kernel_bytes" ] || {
		echo "Kernel image is $ksize bytes; the stock loader reads $pt1570_oem_kernel_bytes" >&2
		return 1
	}
	[ "$rsize" -gt 0 ] && [ "$rsize" -le "$pt1570_oem_rootfs_bytes" ] || {
		echo "Rootfs image is $rsize bytes; the OEM slot holds $pt1570_oem_rootfs_bytes" >&2
		return 1
	}

	# Zero-fill to the fixed read size: the loader ignores the volume's real
	# length, and an unterminated LZMA stream would make 'lzmadec' fail.
	pad=$(( (pt1570_oem_kernel_bytes - ksize + 1023) / 1024 ))
	dd if=/dev/zero bs=1024 count="$pad" >> "$ktmp" 2>/dev/null || return 1

	# The device tree is read at a fixed 256 KiB too, and only if it is shipped.
	if tar tf "$image" | grep -qx "$board_dir/dtb" && [ -n "$dvol" ]; then
		tar xOf "$image" "$board_dir/dtb" > "$dtmp" || return 1
		dsize=$(wc -c < "$dtmp")
		[ "$dsize" -gt 0 ] && [ "$dsize" -le "$pt1570_oem_dtb_bytes" ] || {
			echo "DTB is $dsize bytes; the OEM slot holds $pt1570_oem_dtb_bytes" >&2
			return 1
		}
		pad=$(( (pt1570_oem_dtb_bytes - dsize + 1023) / 1024 ))
		dd if=/dev/zero bs=1024 count="$pad" >> "$dtmp" 2>/dev/null || return 1
	fi

	nand_remove_ubiblock "$rvol" || return 1
	nand_remove_ubiblock "$kvol" || return 1
	[ -s "$dtmp" ] && { ubiupdatevol "/dev/$dvol" "$dtmp" || return 1; }
	ubiupdatevol "/dev/$rvol" "$rtmp" || return 1
	ubiupdatevol "/dev/$kvol" "$ktmp" || return 1
	rm -f "$ktmp" "$rtmp" "$dtmp"
	echo "PT1570-R10: wrote ubi_k$slot (6 MiB padded) and ubi_r$slot" >&2

	# nand_do_upgrade() normally reboots from nand_do_upgrade_success(); this
	# path replaces it, so it has to finish the job itself. The overlay is
	# untouched, which is why the configuration survives without a restore.
	sync
	umount -a
	reboot -f
}

platform_do_upgrade() {"""
),
(
"""	CI_KERNPART="ubi_k$slot"
	CI_ROOTPART="ubi_r$slot"
	nand_do_upgrade "$1\"""",
"""	CI_KERNPART="ubi_k$slot"
	CI_ROOTPART="ubi_r$slot"
	if pt1570_oem_boot; then
		pt1570_oem_upgrade "$1" "$slot"
		return $?
	fi
	nand_do_upgrade "$1\""""
),
])

print("done")
