#!/bin/sh
# Test-only: the checks in arena_put and the allocators must catch a wrong strlen, so
# this writes a copy of zfind.asm with one extra instruction. The mutant lives under
# build/; src/ stays correct.
# usage: inject_fault.sh SOURCE DEST len|shr|over|size|alloc
set -eu
src=${1:?usage: inject_fault.sh SOURCE DEST len|shr|over|size|alloc}
dest=${2:?usage: inject_fault.sh SOURCE DEST len|shr|over|size|alloc}
mode=${3:-len}
case $mode in
  len)   fn=arena_put;       m1=mov;  m2=r13,;  when=after;  ins='sub r13, 8' ;;
  shr)   fn=arena_put;       m1=mov;  m2=r13,;  when=after;  ins='shr r13, 1' ;;
  over)  fn=arena_put;       m1=mov;  m2=r13,;  when=after;  ins='add r13, 100' ;;
  size)  fn=arena_put;       m1=call; m2=salloc; when=before; ins='mov rdi, -4096' ;;
  alloc) fn=new_text_record; m1=call; m2=alloc;  when=before; ins='mov rdi, -4096' ;;
  *) echo "inject_fault.sh: mode must be len, shr, over, size or alloc" >&2; exit 2 ;;
esac
tmp=$(mktemp "$dest.XXXXXX") || exit 1
trap 'rm -f "$tmp"' EXIT
grep -Fxq "$fn:" "$src" || {
    echo "inject_fault.sh: $src has no '$fn:' label." >&2
    echo "inject_fault.sh: the function moved or was renamed; update the mode table." >&2
    exit 1
}
awk -v fn="$fn:" -v m1="$m1" -v m2="$m2" -v when="$when" -v ins="$ins" '
  $0 == fn { infn = 1; print; next }
  infn && ($0 ~ /^[A-Za-z_][A-Za-z0-9_.]*:/ || $0 ~ /\.end_func:/) { infn = 0 }
  infn && done == 0 && $1 == m1 && $2 == m2 {
      if (when == "before") print "    " ins
      print
      if (when == "after") print "    " ins
      done = 1
      next
  }
  { print }
  END {
      if (done != 1) {
          print "inject_fault.sh: no \"" m1 " " m2 "\" inside " fn " in the source." > "/dev/stderr"
          print "inject_fault.sh: the anchor moved; update the mode table before trusting this build." > "/dev/stderr"
          exit 1
      }
  }
' "$src" > "$tmp"
mv "$tmp" "$dest"
trap - EXIT
