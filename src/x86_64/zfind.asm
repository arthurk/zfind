default rel                     ; x86-64: address labels RIP-relative, not via implicit ABS

; zfind, "simple search" use case only:
;   zfind [--help] [--null] [--case-sensitive | --case-insensitive]
;         [--gitignore | --no-gitignore] [--hidden | --no-hidden]
;         [--limit N] [--type f|d] [--] [PATTERN]
; PATTERN is a literal substring (case-insensitive unless it contains A-Z,
; "smart case"); regex and glob syntax are not implemented.
; Every entry is searched, including hidden names and dot-directories like
; .git. --hidden states the default explicitly; --no-hidden skips them. The
; two options conflict. --no-gitignore states the default (no .gitignore
; rules, .git not pruned) explicitly; --gitignore applies .gitignore rules
; when cwd contains .git and prunes .git. The two options conflict.
; .gitignore rules apply only when --gitignore is given and cwd contains
; .git (the existing shortcut; no ancestor walk); with the flag, the .git
; entry itself is pruned - never printed, never walked - because git never
; tracks anything under it. --no-gitignore leaves .git as an ordinary hidden
; entry. The pattern is matched against the name only.
; Use -- before a PATTERN that starts with -.
; shortcut: no path arguments, no regex/glob, and none of the
;   ignore sources beyond .gitignore in the walked tree itself (no parent
;   dirs, no global gitignore, no .ignore, no [] or \ in patterns);
;   add use-case by use-case.
;
; The walk is parallel.  Output is an explicit tree of groups.  A group's
; items are ordered text records and child groups; the writer walks that tree
; depth-first, left-to-right, so the single-threaded byte order is reproduced
; exactly while producers run ahead.  A global LIFO stack holds groups that are
; ready to run; any worker may claim one.  The root group is the dispatcher: it
; pre-scans a wide level, creates and pushes every child group, then links them
; in sorted order.  A group whose root was narrow enough may split again; for a
; very wide root, child groups walk their subtrees inline so the parent-fd
; relative-open optimisation is not lost to thousands of short groups.
; Directories with at least RANGE_MIN entries are copied into contiguous sort
; slices, one group per slice, which also covers huge all-files directories
; such as /nix/store/.links.
;
; Memory is bounded two ways.  Names, sort tables and ignore rules live in a
; per-thread scratch region with stack discipline: walk_dir puts the scratch top
; back when it returns, so a directory's entries die with its frame.  Output
; blocks go back to the thread that made them as soon as the writer has written
; them; a producer more than BLK_LIMIT blocks ahead waits, except the record the
; writer is on, so backpressure cannot deadlock.  Records, groups, paths,
; ignore-chain nodes and range copies are arena-backed and never freed; the
; block limit and SPLIT_CAP are the bounded-memory knobs.
; shortcut: wide splitting starts at the root; if that first level has more
;   than NESTED_WIDTH_MAX children, child groups do not split again.  This
;   keeps /nix/store at one group per package while a small repo can still
;   split deeper.  A directory with an enormous number of entries uses range
;   slices instead of one group per child.
; shortcut: the writer streams the record it is on as it grows (polling every
;   5 ms for new lines) but an unfinished record further ahead is not written
;   until it is the current one, which is what keeps the order exact.
;
; --limit N is enforced by the writer alone: out_put counts the separators in the
; bytes it is handed and, in the call that reaches the Nth, keeps the bytes
; through it, flushes and exit_groups. No stop flag is needed and the prefix is
; the DFS order above, so it is deterministic. Workers that ran ahead of the
; writer die with the process.
; shortcut: an error raised by a worker that ran ahead of the writer (tree or
;   directory too large) still exits 1, so a capped run on a tree that triggers
;   one may exit 0 or 1 depending on timing.
; Requires an x86-64-v3 CPU (AVX2, POPCNT): out_put's separator count uses them.

%define AT_FDCWD    -100
%define PATH_MAX    4096
%define ENT_BUF     32768
%macro LAST_BATCH 0                 ; rax = bytes returned -> r13d = 1 if that was all
%ifndef NO_EOF_SHORTCUT
    ; A local filesystem fills the buffer until the next entry does not fit. A
    ; batch that left room for the largest possible entry therefore ended the
    ; directory, and the getdents64 that would only return 0 can be skipped.
    ; (NFS and FUSE may return short batches: build with -DNO_EOF_SHORTCUT.)
    xor r13d, r13d
    cmp rax, ENT_BUF - DIRENT_MAX
    setbe r13b
%else
    xor r13d, r13d
%endif
%endmacro
%define DIRENT_MAX  4128             ; room a getdents64 batch must have left to
                                     ;   prove it was the last: any d_name up
                                     ;   to 4096 bytes fits
%define IGBUF_SZ    65536            ; .gitignore bytes read per directory
                                      ; shortcut: only IGBUF_SZ - 1 bytes of a
                                      ;   .gitignore are read, the rest is
                                      ;   dropped; raise this if a real file
                                      ;   ever exceeds that
%define IG_MAX      1024             ; rules kept at once (per thread)
%define ARENA_SZ    (1 << 30)        ; records, groups, output blocks: one mmap
                                      ;   (chunks are handed out on demand,
                                      ;    pages fault in on use)
%ifndef SCRATCH_SZ
%define SCRATCH_SZ  (1 << 29)        ; per thread: names, sort table, rules
%endif
                                      ;   (halved at startup if the kernel
                                      ;    refuses the whole mapping)
%define CHUNK       (1 << 20)        ; arena reservation granularity
%define STACK_SZ    (1 << 20)        ; one worker stack
%define MAX_THREADS 16
%ifndef BLK_SZ
%define BLK_SZ      16384            ; one output block, header included
%endif
%ifndef BLK_LIMIT
%define BLK_LIMIT   14336            ; blocks in flight before producers wait
%endif
%define BLK_HDR     32
%define FD_KEEP_MAX  64               ; directories one thread keeps open while recursing
%define SORT_RUN    16               ; insertion-sort runs this long, merge above
%define BLK_DATA    (BLK_SZ - BLK_HDR)
%define BLK_NEXT    0                ; next block of the record / free-list link
%define BLK_USED    8                ; bytes of data written so far
%define BLK_OWNER   16               ; thread that gets it back
%define OUT_SZ      (1 << 16)
%define CLONE_FLAGS  0x50f00         ; VM|FS|FILES|SIGHAND|THREAD|SYSVSEM
%define FUTEX_WAIT_PRIV 128          ; FUTEX_WAIT | FUTEX_PRIVATE_FLAG
%define FUTEX_WAKE_PRIV 129
%define WAKE_ALL    0x7fffffff

%define F_NEG       1                ; ignore rule flags
%define F_DIR       2
%define F_ANCHOR    4

; per-thread state; one instance per worker plus the main thread's, all from
; one mmap. r14 points at the caller's instance in every function that uses it.
struc TSTATE
  .path_buf:  resb PATH_MAX         ; current directory path relative to cwd
  .statbuf:   resb 144
  .ent_buf:   resb ENT_BUF          ; one getdents64 batch
  .igbuf:     resb IGBUF_SZ         ; one buffer per thread: load_ignore never
                                    ;   re-enters, it reads and parses one file
  .ig_rules:  resb 32 * IG_MAX      ; this level's rules (parsing scratch)
  alignb 64
  .id:        resq 1                ; thread number: owner of the blocks it makes
  .my_base:   resq 1                ; arena chunk reservations
  .my_top:    resq 1
  .my_end:    resq 1
  .sc_top:    resq 1                ; scratch bump pointer; walk_dir restores it
  .sc_end:    resq 1
  .path_len:  resq 1
  .f_deep:    resq 1
  .ig_nrules: resq 1
  .ig_chain:  resq 1                ; ancestor rules, innermost node first
  .cur_task:  resq 1                ; record we append lines to
  .cur_blk:   resq 1                ; its last block
  .cur_grp:   resq 1                ; group whose item list we append to
  .no_split:  resq 1                ; 1 = this walk must not wide-split
  .tab_ptr:   resq 1                ; {key, name} table (16 bytes each) of the current dir
  .tab_n:     resq 1
  .tab_cap:   resq 1
  .blk_free:  resq 1                ; blocks the writer gave back to us
  .parent_fd: resq 1                ; handed to the next walk_dir: open relative to it (-1: by path)
  .child_name: resq 1               ; ... the entry name to open there
endstruc
%define path_buf   TSTATE.path_buf
%define statbuf    TSTATE.statbuf
%define ent_buf    TSTATE.ent_buf
%define igbuf      TSTATE.igbuf
%define ig_rules   TSTATE.ig_rules
%define path_len   TSTATE.path_len
%define f_deep     TSTATE.f_deep
%define ig_nrules  TSTATE.ig_nrules
%define ig_chain   TSTATE.ig_chain
%define cur_task   TSTATE.cur_task
%define cur_blk    TSTATE.cur_blk
%define cur_grp    TSTATE.cur_grp

%define TSTATE_STRIDE ((TSTATE_size + 63) & ~63)

; record (arena): one unit of output.  It holds a chain of output blocks and
; is linked into its owning group before the first block is requested.
%define REC_HEAD     0               ; first output block (0 until there is output)
%define REC_DONE     8               ; dword, set after the last block is final
%define REC_NEXT    16               ; tagged next item in the owning group
%define REC_SZ      32
; group (arena): one node of the output tree and one unit of work
%define GRP_WORK_NEXT  0             ; LIFO work-stack link
%define GRP_ITEM_NEXT  8             ; tagged next item in the parent's list
%define GRP_STATE     16              ; dword: 0 NEW, 1 RUNNING, 2 DONE
%define GRP_KIND      20              ; dword: 0 directory, 1 range
%define GRP_FIRST     24              ; tagged first item (0 = none)
%define GRP_LAST      32              ; tagged last item (producer-private)
%define GRP_PATH      40              ; arena path; empty root uses 0
%define GRP_PATHLEN   48
%define GRP_CHAIN     56              ; arena ignore chain
%define GRP_TAB       64              ; range: copied {key,name} table
%define GRP_NAMES     72              ; range: copied name records
%define GRP_N         80              ; range: table entries
%define GRP_SZ        96
%define GRP_NEW       0
%define GRP_RUNNING   1
%define GRP_DONE      2
%define GRP_DIR       0
%define GRP_RANGE     1
%define GRP_TAG       1
%define EMIT_STACK_MAX 8192
%define RANGE_MIN     (1 << 17)
%ifndef SPLIT_CAP
%define SPLIT_CAP     8192             ; upper bound on wide-split groups
%endif
%define NESTED_WIDTH_MAX 20           ; wider root: child groups do not split again
%define CHUNK_MIN     (1 << 11)
%define MAX_RANGE_CHUNKS 128
; ignore-rule chain node (arena)
%define NODE_RULES  0
%define NODE_PARENT 8
%define NODE_NRULES 16
%define NODE_SZ     24

section .rodata
    dot         db '.', 0
    gitname     db '.git', 0
    igname      db '.gitignore', 0
    e_arg       db "zfind: unexpected argument", 10
    e_arg_e:
    e_limit     db "zfind: invalid limit", 10
    e_limit_e:
    e_type      db "zfind: expected f or d after --type", 10
    e_type_e:
    e_flag      db "zfind: unknown option", 10
    e_flag_e:
    e_write     db "zfind: write error", 10
    e_write_e:
    e_big       db "zfind: tree too large", 10
    e_big_e:
    e_scratch   db "zfind: directory too large", 10
    e_scratch_e:
    e_case      db "zfind: cannot combine --case-sensitive and --case-insensitive", 10
    e_case_e:
    e_hid       db "zfind: cannot combine --hidden and --no-hidden", 10
    e_hid_e:
    e_ig        db "zfind: cannot combine --gitignore and --no-gitignore", 10
    e_ig_e:
    h_long      db "--help", 0
    z_long      db "--null", 0
    cs_long     db "--case-sensitive", 0
    ci_long     db "--case-insensitive", 0
    l_long      db "--limit", 0
    l_eq        db "--limit=", 0     ; 8 bytes of prefix
    t_long      db "--type", 0
    t_file      db "f", 0
    t_dir       db "d", 0
    g_long      db "--gitignore", 0
    nog_long    db "--no-gitignore", 0
    hidden_long db "--hidden", 0
    no_long     db "--no-hidden", 0
    help_msg    db "usage: zfind [--help] [--null] [--case-sensitive | --case-insensitive]", 10
               db "             [--gitignore | --no-gitignore] [--hidden | --no-hidden]", 10
               db "             [--limit N] [--type f|d] [--] [PATTERN]", 10, 10
               db "Search the current directory tree for entries whose name contains", 10
               db "PATTERN, a literal substring (no regex or glob). By default, matching", 10
               db "is case-insensitive unless PATTERN contains an uppercase ASCII letter", 10
               db "(smart case). Use --case-sensitive or --case-insensitive to override this;", 10
               db "combining both is an error. Case folding affects only PATTERN (ASCII", 10
               db "only) and does not apply to .gitignore rules. Without a pattern every", 10
               db "entry is listed unless --type restricts the result types.", 10, 10
               db "Options must appear before PATTERN. --limit and --type consume the next", 10
               db "token greedily as their value. -- ends options; any following argument", 10
               db "is treated as PATTERN. --help prints this help and exits 0 anywhere", 10
               db "before --, including after PATTERN or where an option value is expected.", 10, 10
               db "Hidden entries are searched like any other, .git included; --no-hidden", 10
               db "skips entries whose name starts with . and everything inside them.", 10
               db "--hidden states the default explicitly; combining the two is an error.", 10
               db "Without either gitignore flag, rules are off and .git is not pruned.", 10
               db "--no-gitignore states this default explicitly. --gitignore applies", 10
               db "rules when the current directory contains .git and skips .git itself.", 10
               db "Combining the two gitignore flags is an error. Git does not track anything", 10
               db "under .git. Entries are sorted by name within each directory; output", 10
               db "follows depth-first order.", 10, 10
               db "  --help              show this help and exit", 10
               db "  --null              separate entries with NUL, not newline", 10
               db "  --case-sensitive    match pattern case-sensitively", 10
               db "  --case-insensitive  match pattern case-insensitively (ASCII A-Z)", 10
               db "  --gitignore         respect .gitignore rules when cwd contains .git; skip .git", 10
               db "  --no-gitignore      do not apply .gitignore rules (the default)", 10
               db "  --hidden            search hidden entries (the default)", 10
               db "  --no-hidden         skip entries whose name starts with .", 10
               db "  --limit N           print at most N results, then stop (0 = none)", 10
               db "  --type f|d          repeatable; print regular files and/or directories", 10
               db "  --                  end options; a following argument is the PATTERN", 10
    help_msg_e:
    align 8
    poll_ts     dq 0, 5000000         ; 5 ms: how stale streamed output can get

section .bss
    arena_base: resq 1
    g_resv:     resq 1                ; next free arena byte (xadd'd)
    pat_ptr:    resq 1
    pat_len:    resq 1
    pat_ci:     resq 1                ; 1 = fold case (both filename and pattern bytes)
    nul_sep:    resq 1                ; 1 = separate entries with NUL, not newline
    lim_left:   resq 1                ; results left before the writer stops; -1 = no limit
    res_ig:     resq 1                ; 0 = default, 1 = --gitignore, 2 = --no-gitignore
    hid_mode:   resb 1                ; 0 = default, 1 = --hidden, 2 = --no-hidden
    case_mode:  resb 1                ; 0 = smart (default), 1 = forced sensitive, 2 = forced insensitive
    res_type:   resb 32                ; d_type bitset: DT_DIR=4, DT_REG=8
    type_seen:  resb 1
    git_repo:   resq 1
    tstate_map: resq 1
    stacks:     resq 1
    scratch_map: resq 1
    scratch_sz: resq 1                ; per thread
    nthreads:   resq 1                ; walking threads; the main thread writes
    split_min:  resq 1                ; dispatch a level with this many subdirs
    fd_keep:    resq 1                ; per-thread budget of directories kept open
    rl_buf:     resq 2
    work_head:  resq 1                ; LIFO of NEW groups
    pending:    resq 1                ; groups not yet DONE
    group_count: resq 1               ; wide-split groups created
    root_width: resq 1                ; dirs at the first split level
    alignb 64
    root_grp:   resb GRP_SZ           ; static root group
    emit_stack: resb EMIT_STACK_MAX * 24
    alignb 8                          ; futex words must be naturally aligned
    ev_seq:     resq 1                ; dword futex word: work / group state changes
    ev_waiters: resq 1
    bp_seq:     resq 1                ; dword futex word: the writer made progress
    bp_waiters: resq 1
    blk_out:    resq 1                ; blocks handed out and not yet written
    emit_cur:   resq 1                ; record the writer is on (never throttled)
    out_len:    resq 1
    statbuf0:   resb 144              ; only _start's .git check, before any thread
    cpuset:     resb 128
    out_buf:    resb OUT_SZ
    alignb 64
    ret_heads:  resb 64 * MAX_THREADS ; per owner: blocks the writer returned

section .text
    global _start

_start:
    mov rbx, [rsp]                   ; argc
    lea rbp, [rsp+8]                 ; argv

    ; Pre-scan argv[1..argc) for --help before the first "--"
    mov r12, 1
.prescan_loop:
    cmp r12, rbx
    jae .prescan_done
    mov rdi, [rbp+r12*8]
    cmp byte [rdi], '-'
    jne .prescan_next
    cmp byte [rdi+1], '-'
    jne .prescan_next
    cmp byte [rdi+2], 0
    je .prescan_done                 ; stop scanning at the first "--"
    lea rsi, [h_long]
    call streq
    je .help
    jmp .prescan_next
.prescan_next:
    inc r12
    jmp .prescan_loop

.prescan_done:
    mov qword [lim_left], -1
    xor r13d, r13d
    mov r12, 1
.args_loop:
    cmp r12, rbx
    jae .args_done
    mov rdi, [rbp+r12*8]
    cmp byte [rdi], '-'
    jne .arg_pattern
    cmp byte [rdi+1], 0
    je .arg_pattern                    ; "-" alone

    mov rdi, [rbp+r12*8]
    lea rsi, [z_long]
    call streq
    je .opt_null
    mov rdi, [rbp+r12*8]
    lea rsi, [cs_long]
    call streq
    je .opt_case_sens
    mov rdi, [rbp+r12*8]
    lea rsi, [ci_long]
    call streq
    je .opt_case_insens
    mov rdi, [rbp+r12*8]
    lea rsi, [l_long]
    call streq
    je .opt_limit_next
    mov rdi, [rbp+r12*8]
    lea rsi, [t_long]
    call streq
    je .opt_type
    mov rdi, [rbp+r12*8]
    lea rsi, [l_eq]
    call startswith
    je .opt_limit_eq
    mov rdi, [rbp+r12*8]
    lea rsi, [g_long]
    call streq
    je .opt_git
    mov rdi, [rbp+r12*8]
    lea rsi, [nog_long]
    call streq
    je .opt_nogit
    mov rdi, [rbp+r12*8]
    lea rsi, [hidden_long]
    call streq
    je .opt_hidden

    mov rdi, [rbp+r12*8]
    lea rsi, [no_long]
    call streq
    je .opt_nohid

    mov rdi, [rbp+r12*8]
    cmp byte [rdi+1], '-'
    jne bad_flag                       ; unknown short flag
    cmp byte [rdi+2], 0
    je .arg_end_opts                   ; "--"
    jmp bad_flag                       ; unknown long flag

.opt_case_sens:
    cmp byte [case_mode], 2
    je bad_case_comb
    mov byte [case_mode], 1
    inc r12
    jmp .args_loop

.opt_case_insens:
    cmp byte [case_mode], 1
    je bad_case_comb
    mov byte [case_mode], 2
    inc r12
    jmp .args_loop

.opt_type:
    inc r12
    cmp r12, rbx
    jae bad_type
    mov rdi, [rbp+r12*8]
    lea rsi, [t_file]
    call streq
    je .type_file
    mov rdi, [rbp+r12*8]
    lea rsi, [t_dir]
    call streq
    jne bad_type
    mov byte [type_seen], 1
    bts qword [res_type], 4
    jmp .type_next
.type_file:
    mov byte [type_seen], 1
    bts qword [res_type], 8
.type_next:
    inc r12
    jmp .args_loop

.opt_null:
    mov qword [nul_sep], 1
    inc r12
    jmp .args_loop

.opt_git:
    cmp qword [res_ig], 2
    je bad_ig_comb
    mov qword [res_ig], 1
    inc r12
    jmp .args_loop

.opt_nogit:
    cmp qword [res_ig], 1
    je bad_ig_comb
    mov qword [res_ig], 2
    inc r12
    jmp .args_loop

.opt_hidden:
    cmp byte [hid_mode], 2
    je bad_hid_comb
    mov byte [hid_mode], 1
    inc r12
    jmp .args_loop

.opt_nohid:
    cmp byte [hid_mode], 1
    je bad_hid_comb
    mov byte [hid_mode], 2
    inc r12
    jmp .args_loop

.opt_limit_next:
    inc r12
    cmp r12, rbx
    jae bad_limit
    mov rdi, [rbp+r12*8]
    call parse_u64
    jc bad_limit
    mov [lim_left], rax
    inc r12
    jmp .args_loop

.opt_limit_eq:
    mov rdi, [rbp+r12*8]
    add rdi, 8                         ; len("--limit=")
    call parse_u64
    jc bad_limit
    mov [lim_left], rax
    inc r12
    jmp .args_loop

.arg_end_opts:
    inc r12
    cmp r12, rbx
    jae .args_done
    mov r13, [rbp+r12*8]
    inc r12
    cmp r12, rbx
    jne bad_args
    jmp .args_done

.arg_pattern:
    mov r13, [rbp+r12*8]
    inc r12
    cmp r12, rbx
    jne bad_args
    jmp .args_done

.args_done:
    test r13, r13
    jz .no_pat
    mov [pat_ptr], r13
    mov rdi, r13
    call strlen
    mov [pat_len], rax
    jmp .pat_ready
.no_pat:
    mov qword [pat_len], 0
    jmp .pat_ready

.help:                                 ; write the usage, then leave: no walk
    mov edi, 1
    lea rsi, [help_msg]
    mov edx, help_msg_e - help_msg
    mov rax, 1                       ; sys_write
    syscall
    xor edi, edi
    mov rax, 231                     ; exit_group
    syscall
.exit_zero:                            ; --limit 0: nothing to print, nothing to walk
    xor edi, edi
    mov eax, 231                     ; exit_group
    syscall
.pat_ready:
    cmp byte [case_mode], 1
    je .forced_sensitive
    cmp byte [case_mode], 2
    je .forced_insensitive
    ; smart case: one A-Z anywhere in the pattern makes it case-sensitive
    mov rsi, [pat_ptr]
    mov rcx, [pat_len]
    xor edx, edx
.scan:
    test rcx, rcx
    jz .sc_done
    mov al, [rsi]
    cmp al, 'A'
    jb .sc_next
    cmp al, 'Z'
    jbe .sensitive
.sc_next:
    inc rsi
    dec rcx
    jmp .scan
.sensitive:
    mov edx, 1
.sc_done:
    xor edx, 1
    mov [pat_ci], rdx
    jmp .case_done
.forced_sensitive:
    mov qword [pat_ci], 0
    jmp .case_done
.forced_insensitive:
    mov qword [pat_ci], 1
.case_done:
    cmp qword [lim_left], 0          ; --limit 0 exits before fstat, mmap and threads, so
    je .exit_zero                    ;   from here on lim_left is -1 or positive

    ; .gitignore applies only inside a repository, and only when
    ; --gitignore was given (res_ig == 1). State 0 (default) and state 2
    ; (--no-gitignore) both skip .git detection, rule loading and the .git
    ; prune: git_repo stays 0 and every gate downstream reads 0.
    cmp qword [res_ig], 1
    jne .norepo
    mov rax, 262                     ; newfstatat
    mov rdi, AT_FDCWD
    lea rsi, [gitname]
    lea rdx, [statbuf0]
    xor r10d, r10d
    syscall
    test rax, rax
    js .norepo
    mov qword [git_repo], 1
.norepo:
    xor edi, edi                     ; arena: one big mmap instead of a static
    mov rsi, ARENA_SZ                ; buffer, handed out in chunks, so names
    mov edx, 3                       ; + output cannot overflow it
    mov r10, 0x4022                  ; MAP_PRIVATE|MAP_ANONYMOUS|MAP_NORESERVE
    mov r8, -1
    xor r9d, r9d
    mov eax, 9                       ; mmap
    syscall
    test rax, rax
    js big
    mov [arena_base], rax
    mov qword [g_resv], 0            ; g_resv counts offsets, not addresses

    call cpu_count
    mov [nthreads], rax
    ; Subdirectories are opened relative to their parent's fd, which saves the
    ; kernel walking the whole path again, so a thread keeps up to FD_KEEP_MAX
    ; directories open while it recurses - fewer if the descriptor limit would
    ; be at risk: (soft limit - 32) / threads. Past the budget, and if the limit
    ; cannot be read, directories are opened by path as before.
    mov edi, 7                       ; RLIMIT_NOFILE
    lea rsi, [rl_buf]
    mov eax, 97                      ; getrlimit
    syscall
    mov rax, [rl_buf]
    xor ecx, ecx
    cmp rax, 64
    jbe .fdk
    sub rax, 32
    xor edx, edx
    div qword [nthreads]
    mov rcx, rax
    cmp rcx, FD_KEEP_MAX
    jbe .fdk
    mov ecx, FD_KEEP_MAX
.fdk:
%ifdef NO_RELATIVE_OPEN
    xor ecx, ecx
%endif
    mov [fd_keep], rcx
    mov eax, 2                       ; dispatch any level with 2+ subdirs
    mov [split_min], rax

    ; static root group: the whole output tree and the first work item
    lea rax, [root_grp]
    mov qword [rax+GRP_WORK_NEXT], 0
    mov qword [rax+GRP_ITEM_NEXT], 0
    mov dword [rax+GRP_STATE], GRP_NEW
    mov dword [rax+GRP_KIND], GRP_DIR
    mov qword [rax+GRP_FIRST], 0
    mov qword [rax+GRP_LAST], 0
    mov qword [rax+GRP_PATH], 0
    mov qword [rax+GRP_PATHLEN], 0
    mov qword [rax+GRP_CHAIN], 0
    mov qword [work_head], rax
    mov qword [pending], 1

    ; one state block per thread, zeroed by the kernel
    xor edi, edi
    mov rsi, [nthreads]
    imul rsi, TSTATE_STRIDE
    mov edx, 3
    mov r10, 0x4022
    mov r8, -1
    xor r9d, r9d
    mov eax, 9
    syscall
    test rax, rax
    js big
    mov [tstate_map], rax
    ; one stack per thread
    xor edi, edi
    mov rsi, [nthreads]
    imul rsi, STACK_SZ
    mov edx, 3
    mov r10, 0x4022
    mov r8, -1
    xor r9d, r9d
    mov eax, 9
    syscall
    test rax, rax
    js big
    mov [stacks], rax
    ; one scratch region per thread. Untouched pages cost nothing, but a strict
    ; overcommit policy counts them, so halve the size until the kernel agrees
    mov rbx, SCRATCH_SZ
.scratch_try:
    xor edi, edi
    mov rsi, [nthreads]
    imul rsi, rbx
    mov edx, 3
    mov r10, 0x4022
    mov r8, -1
    xor r9d, r9d
    mov eax, 9
    syscall
    test rax, rax
    jns .scratch_ok
    shr rbx, 1
    cmp rbx, (1 << 22)               ; under 4 MiB per thread: give up
    jae .scratch_try
    jmp big
.scratch_ok:
    mov [scratch_map], rax
    mov [scratch_sz], rbx

    xor r12d, r12d                   ; thread id; the child keeps r12/r13
.spawn_loop:
    cmp r12, [nthreads]
    jae .spawned
    mov r13, r12
    imul r13, TSTATE_STRIDE
    add r13, [tstate_map]            ; its state block
    mov rax, r12
    imul rax, [scratch_sz]
    add rax, [scratch_map]           ; its scratch region
    mov [r13+TSTATE.sc_top], rax
    add rax, [scratch_sz]
    sub rax, 16                      ; tail guard: sort reads 8 bytes past a name
    mov [r13+TSTATE.sc_end], rax
    mov rsi, r12
    inc rsi
    imul rsi, STACK_SZ
    add rsi, [stacks]                ; top of its stack
    mov edi, CLONE_FLAGS
    xor edx, edx
    xor r10d, r10d
    xor r8d, r8d
    mov eax, 56                      ; clone
    syscall
    test rax, rax
    jz thread_main
    inc r12
    jmp .spawn_loop
.spawned:
    call emit_main                   ; the main thread only writes
    xor edi, edi                     ; every normal path exits 0, even no match
    mov rax, 231                     ; exit_group
    syscall

; strlen: rdi = str -> rax = len
strlen:
    xor rax, rax
.loop:
    cmp byte [rdi+rax], 0
    je .done
    inc rax
    jmp .loop
.done:
    ret

; streq: rdi, rsi = two NUL-terminated strings -> ZF = 1 if they are equal
streq:
    xor ecx, ecx
.loop:
    mov al, [rdi+rcx]
    cmp al, [rsi+rcx]
    jne .ret                         ; ZF = 0 from the cmp
    test al, al
    jz .ret                          ; both ended here: ZF = 1
    inc ecx
    jmp .loop
.ret:
    ret

; parse_u64: rdi = NUL-terminated decimal -> rax = value, CF=1 on error
parse_u64:
    xor eax, eax
    xor ecx, ecx                       ; digits seen
.loop:
    movzx edx, byte [rdi]
    test dl, dl
    jz .done
    sub edx, '0'
    cmp edx, 9
    ja .bad
    mov r8, rdx                        ; digit
    mov rsi, 10
    mul rsi                            ; rdx:rax = rax * 10
    test rdx, rdx
    jnz .bad                           ; overflow
    add rax, r8
    jc .bad
    inc ecx
    inc rdi
    jmp .loop
.done:
    test ecx, ecx
    jz .bad                            ; empty value
    clc
    ret
.bad:
    stc
    ret

; startswith: rdi = string, rsi = prefix -> ZF=1 if string begins with prefix
startswith:
    xor ecx, ecx
.loop:
    mov al, [rsi+rcx]
    test al, al
    jz .yes
    cmp al, [rdi+rcx]
    jne .no
    inc ecx
    jmp .loop
.yes:
    xor eax, eax                       ; ZF=1
    ret
.no:
    or eax, 1                          ; ZF=0
    ret

; cpu_count -> rax = walking thread count, 2..MAX_THREADS (the main thread
; writes and is not counted). -DFORCE_THREADS=n pins it for stress tests.
cpu_count:
%ifdef FORCE_THREADS
    mov eax, FORCE_THREADS
    ret
%endif
    mov eax, 204                     ; sched_getaffinity
    xor edi, edi                     ; pid 0 = this thread
    mov esi, 128                     ; cpusetsize
    lea rdx, [cpuset]                ; mask
    syscall
    test rax, rax
    js .fallback
    xor ecx, ecx
    mov rsi, 128
.bits:
    test rsi, rsi
    jz .got
    dec rsi
    bt [cpuset], rsi
    adc ecx, 0
    jmp .bits
.got:
    test ecx, ecx
    jz .fallback
    cmp ecx, MAX_THREADS
    jbe .ok
    mov ecx, MAX_THREADS
.ok:
    cmp ecx, 2
    jae .ok2
    mov ecx, 2
.ok2:
    mov rax, rcx
    ret
.fallback:
    mov eax, 4
    ret

; thread_main: entered by a fresh clone with r12 = its id, r13 = its state block
; and its own stack.  Every thread claims groups from the shared work stack
; until pending is zero; whoever claims the root group performs its prepass and
; publishes the first level for the others.
thread_main:
    mov r14, r13
    mov [r14+TSTATE.id], r12
    mov qword [r14+TSTATE.parent_fd], -1
    call run_workers
    mov eax, 60                      ; SYS_exit: leave the other threads alone
    xor edi, edi
    syscall

; run_workers: claim NEW groups from the LIFO until pending is zero and none
; are left.  The snapshot-before-check protocol prevents lost wake-ups.
run_workers:
.loop:
    mov edx, [ev_seq]
    cmp qword [pending], 0
    jne .try
    cmp qword [work_head], 0
    je .done
.try:
    call pop_group
    test rax, rax
    jz .sleep
    mov rdi, rax
    xor esi, esi                     ; path_ready = 0
    call walk_group
    jmp .loop
.sleep:
    xor r10d, r10d
    call ev_sleep
    jmp .loop
.done:
    ret

; pop_group -> rax = a NEW group, now RUNNING, or 0.  Stale stack entries for
; groups that another worker already claimed, or that finished, are skipped.
pop_group:
.retry:
    mov rax, [work_head]
    test rax, rax
    jz .none
    mov rcx, [rax+GRP_WORK_NEXT]
    lock cmpxchg [work_head], rcx
    jne .retry
    mov rdx, rax
    xor eax, eax                     ; expected state NEW
    mov ecx, GRP_RUNNING
    lock cmpxchg dword [rdx+GRP_STATE], ecx
    jne .retry
    mov rax, rdx
    ret
.none:
    xor eax, eax
    ret

; notify_ev: something the writer or an idle worker may be waiting for changed.
; The increment is the full barrier that orders the caller's stores before the
; waiter check; a waiter that registers later sees the new value in the futex.
notify_ev:
    lock inc dword [ev_seq]
    cmp dword [ev_waiters], 0
    je .ret
    lea rdi, [ev_seq]
    mov esi, FUTEX_WAKE_PRIV
    mov edx, WAKE_ALL
    mov eax, 202
    syscall
.ret:
    ret

; ev_sleep: edx = the ev_seq value read BEFORE the caller's last look at the
; state, r10 = relative timeout or 0. A change in between makes the futex
; return at once, so no wake-up can be lost.
ev_sleep:
    lock inc dword [ev_waiters]
    lea rdi, [ev_seq]
    mov esi, FUTEX_WAIT_PRIV
    mov eax, 202
    syscall
    lock dec dword [ev_waiters]
    ret

; walk_group: rdi = GRP.  Saves the caller's walk context, executes the group's
; directory or range body, marks it DONE, then helps any child groups that are
; still NEW.  f_deep is deliberately preserved: an inline child runs while the
; caller's directory descriptors are still open.
walk_group:
    push rbx
    push rbp
    push r12
    push r13
    sub rsp, 96
    mov rax, [r14+TSTATE.no_split]
    mov [rsp+0], rax
    mov rax, [r14+path_len]
    mov [rsp+8], rax
    mov rax, [r14+f_deep]
    mov [rsp+16], rax
    mov rax, [r14+ig_nrules]
    mov [rsp+24], rax
    mov rax, [r14+ig_chain]
    mov [rsp+32], rax
    mov rax, [r14+cur_task]
    mov [rsp+40], rax
    mov rax, [r14+cur_blk]
    mov [rsp+48], rax
    mov rax, [r14+cur_grp]
    mov [rsp+56], rax
    mov rax, [r14+TSTATE.parent_fd]
    mov [rsp+64], rax
    mov rax, [r14+TSTATE.child_name]
    mov [rsp+72], rax
    mov rax, [r14+TSTATE.tab_ptr]
    mov [rsp+80], rax
    mov rax, [r14+TSTATE.tab_n]
    mov [rsp+88], rax
    mov rbx, rdi
    mov [r14+cur_grp], rbx
    mov qword [r14+cur_task], 0
    mov qword [r14+cur_blk], 0
    mov rdx, [rbx+GRP_PATHLEN]
    mov rsi, [rbx+GRP_PATH]
    lea rdi, [r14+path_buf]
    test rdx, rdx
    jz .term
    mov rcx, rdx
    rep movsb
.term:
    mov byte [rdi], 0
    mov [r14+path_len], rdx
    mov rax, [rbx+GRP_CHAIN]
    mov [r14+ig_chain], rax
    mov qword [r14+ig_nrules], 0
    mov qword [r14+TSTATE.parent_fd], -1
    lea rax, [root_grp]
    cmp rbx, rax
    je .root_group
    cmp qword [root_width], NESTED_WIDTH_MAX
    jbe .root_group
    mov qword [r14+TSTATE.no_split], 1
.root_group:
    cmp dword [rbx+GRP_KIND], GRP_RANGE
    je .range
    call walk_dir
    jmp .body_done
.range:
    call walk_range
.body_done:
    mov rdi, [r14+cur_task]
    test rdi, rdi
    jz .no_record
    mov qword [r14+cur_task], 0
    mov qword [r14+cur_blk], 0
    call mark_done
.no_record:
    mov dword [rbx+GRP_STATE], GRP_DONE
    lock dec qword [pending]
    call notify_ev
    mov rdi, rbx
    call help_group
    mov rax, [rsp+0]
    mov [r14+TSTATE.no_split], rax
    mov rax, [rsp+8]
    mov [r14+path_len], rax
    mov rax, [rsp+16]
    mov [r14+f_deep], rax
    mov rax, [rsp+24]
    mov [r14+ig_nrules], rax
    mov rax, [rsp+32]
    mov [r14+ig_chain], rax
    mov rax, [rsp+40]
    mov [r14+cur_task], rax
    mov rax, [rsp+48]
    mov [r14+cur_blk], rax
    mov rax, [rsp+56]
    mov [r14+cur_grp], rax
    mov rax, [rsp+64]
    mov [r14+TSTATE.parent_fd], rax
    mov rax, [rsp+72]
    mov [r14+TSTATE.child_name], rax
    mov rax, [rsp+80]
    mov [r14+TSTATE.tab_ptr], rax
    mov rax, [rsp+88]
    mov [r14+TSTATE.tab_n], rax
    add rsp, 96
    pop r13
    pop r12
    pop rbp
    pop rbx
    ret

; push_group: rdi = a fully initialised NEW group.  Publish it on the global
; LIFO and account for it before the notify.
push_group:
    lock inc qword [pending]
.retry:
    mov rax, [work_head]
    mov [rdi+GRP_WORK_NEXT], rax
    lock cmpxchg [work_head], rdi
    jne .retry
    jmp notify_ev

; make_group: rdi = path length.  Copies path_buf[0..len) and the current ignore
; chain into a fully initialised NEW directory group and pushes it.  The group
; is not linked yet; the wide prepass builds all siblings before publishing.
make_group:
    lock inc qword [group_count]
    push rbx
    push rbp
    push r12
    push r13
    mov r12, rdi
    mov rdi, GRP_SZ
    call alloc
    mov rbx, rax
    lea rdi, [r12+1]
    call alloc
    mov r13, rax
    lea rsi, [r14+path_buf]
    mov rdi, r13
    mov rcx, r12
    rep movsb
    mov byte [rdi], 0
    mov qword [rbx+GRP_WORK_NEXT], 0
    mov qword [rbx+GRP_ITEM_NEXT], 0
    mov dword [rbx+GRP_STATE], GRP_NEW
    mov dword [rbx+GRP_KIND], GRP_DIR
    mov qword [rbx+GRP_FIRST], 0
    mov qword [rbx+GRP_LAST], 0
    mov [rbx+GRP_PATH], r13
    mov [rbx+GRP_PATHLEN], r12
    mov rax, [r14+ig_chain]
    mov [rbx+GRP_CHAIN], rax
    mov qword [rbx+GRP_TAB], 0
    mov qword [rbx+GRP_NAMES], 0
    mov qword [rbx+GRP_N], 0
    mov rdi, rbx
    call push_group
    mov rax, rbx
    pop r13
    pop r12
    pop rbp
    pop rbx
    ret

; link_group: rdi = a NEW (or already claimed/done) child group.  Close the
; current text record so the group lands at this exact output position, link
; it, then claim it if it is still NEW.  Claiming means executing it inline;
; the wide prepass has already pushed every sibling, so this cannot serialise
; the level.  If another worker claimed it, that worker owns it.
link_group:
    push rbx
    push r15
    mov r15, rdi
    mov rdi, [r14+cur_task]
    test rdi, rdi
    jz .no_record
    mov qword [r14+cur_task], 0
    mov qword [r14+cur_blk], 0
    call mark_done
.no_record:
    mov rdi, [r14+cur_grp]
    mov rsi, r15
    mov edx, GRP_TAG
    call append_item
    xor eax, eax
    mov ecx, GRP_RUNNING
    lock cmpxchg dword [r15+GRP_STATE], ecx
    jne .done
    mov rdi, r15
    call walk_group
.done:
    pop r15
    pop rbx
    ret

; help_group: rdi = a completed group.  Make one pass over its immutable item
; list and execute any child group still NEW.  Children claimed by another
; worker are simply skipped; text records are not work.
help_group:
    push rbx
    push rbp
    push r12
    push r13
    mov r12, rdi
    mov r13, [r12+GRP_FIRST]
.loop:
    test r13, r13
    jz .done
    mov rax, r13
    test al, 1
    jz .record
    and rax, -2
    mov rbx, rax
    mov r13, [rbx+GRP_ITEM_NEXT]     ; immutable: advance before executing
    xor eax, eax                     ; expected NEW
    mov ecx, GRP_RUNNING
    lock cmpxchg dword [rbx+GRP_STATE], ecx
    jne .loop
    mov rdi, rbx
    call walk_group
    jmp .loop
.record:
    and rax, -2
    mov r13, [rax+REC_NEXT]
    jmp .loop
.done:
    pop r13
    pop r12
    pop rbp
    pop rbx
    ret

; mark_done: rdi = record. Its blocks and HEAD are final; tell the writer.
mark_done:
    mov dword [rdi+REC_DONE], 1
    jmp notify_ev

; emit_main: the main thread.  It walks the output tree in order, writing each
; record as its producer fills it, and flushes whenever it has to wait.
emit_main:
    lea rdi, [root_grp]
    jmp emit_group

; emit_group: rdi = root group.  One explicit stack frame per open group:
;   +0 group, +8 current tagged item, +16 ready byte (24-byte stride).
; The current item is kept until its NEXT is known to be zero and the group is
; observed DONE; STATE is always read before FIRST/NEXT.  See
; IMPL_TASK_SPLIT.md section 7.
emit_group:
    push rbx
    push rbp
    push r12
    push r13
    push r14
    push r15
    mov rbx, rdi
    mov qword [emit_stack+0], rbx
    mov rax, [rbx+GRP_FIRST]
    mov qword [emit_stack+8], rax
    mov byte [emit_stack+16], 0
    xor r12d, r12d                   ; top frame index
.loop:
    lea rbx, [emit_stack]
    mov rax, r12
    imul rax, 24
    add rbx, rax                     ; rbx = frame
    mov rax, [rbx+8]
    test rax, rax
    jz .wait_first
    cmp byte [rbx+16], 0
    jne .advance
    jmp .dispatch
.wait_first:
    mov r13d, [ev_seq]               ; before the checks, see ev_sleep
    mov rcx, [rbx]
    mov edx, [rcx+GRP_STATE]         ; STATE before FIRST
    mov rax, [rcx+GRP_FIRST]
    test rax, rax
    jnz .set_item
    cmp edx, GRP_DONE
    je .pop
    call out_flush
    mov edx, r13d
    xor r10d, r10d
    call ev_sleep
    jmp .loop
.set_item:
    mov [rbx+8], rax
    mov byte [rbx+16], 0
    jmp .dispatch
.advance:
    mov r13d, [ev_seq]
    mov rcx, [rbx]
    mov edx, [rcx+GRP_STATE]         ; STATE before NEXT
    mov rax, [rbx+8]
    test al, 1
    jz .rec_next
    and rax, -2
    mov rax, [rax+GRP_ITEM_NEXT]
    jmp .next_ready
.rec_next:
    and rax, -2
    mov rax, [rax+REC_NEXT]
.next_ready:
    test rax, rax
    jnz .set_next
    cmp edx, GRP_DONE
    je .pop
    call out_flush
    mov edx, r13d
    xor r10d, r10d
    call ev_sleep
    jmp .loop
.set_next:
    mov [rbx+8], rax
    mov byte [rbx+16], 0
    jmp .dispatch
.dispatch:
    mov rax, [rbx+8]
    test al, 1
    jnz .push_child
    mov rbx, rax
    mov [emit_cur], rbx              ; this record's producer is not throttled
    lock inc dword [bp_seq]
    cmp dword [bp_waiters], 0
    je .emit
    lea rdi, [bp_seq]
    mov esi, FUTEX_WAKE_PRIV
    mov edx, WAKE_ALL
    mov eax, 202
    syscall
.emit:
    call emit_record
    lea rcx, [emit_stack]
    mov rax, r12
    imul rax, 24
    add rcx, rax
    mov byte [rcx+16], 1             ; record finished
    jmp .loop
.push_child:
    and rax, -2
    lea rcx, [r12+1]
    cmp rcx, EMIT_STACK_MAX
    jae .overflow
    mov rdi, rax                     ; child group
    lea rax, [r12+1]
    imul rax, 24
    lea rdx, [emit_stack]
    add rdx, rax                     ; child frame
    mov [rdx+0], rdi
    mov rax, [rdi+GRP_FIRST]
    mov [rdx+8], rax
    mov byte [rdx+16], 0
    inc r12
    jmp .loop
.pop:
    test r12, r12
    jz .finish
    dec r12
    lea rcx, [emit_stack]
    mov rax, r12
    imul rax, 24
    add rcx, rax
    mov byte [rcx+16], 1             ; parent's child item finished
    jmp .loop
.overflow:
    jmp big
.finish:
    call out_flush
    pop r15
    pop r14
    pop r13
    pop r12
    pop rbp
    pop rbx
    ret

; emit_record: rbx = record. Writes its blocks as they fill, hands each block
; back to its owner once written, returns when the record is done and drained.
; Clobbers rbp (current block), r13 (bytes of it written), r14 (done snapshot),
; r15 (ev_seq snapshot).
emit_record:
    xor ebp, ebp
    xor r13d, r13d
.iter:
    mov r15d, [ev_seq]               ; before the checks, see ev_sleep
    mov r14d, [rbx+REC_DONE]         ; before the data: a 1 means they are final
    test rbp, rbp
    jnz .blk
    mov rbp, [rbx+REC_HEAD]
    test rbp, rbp
    jz .idle
    xor r13d, r13d
.blk:
    mov rcx, [rbp+BLK_USED]
    cmp rcx, r13
    jbe .drained
    lea rdi, [rbp+BLK_HDR]
    add rdi, r13
    mov rsi, rcx
    sub rsi, r13
    mov r13, rcx
    call out_put
    jmp .iter
.drained:
    mov rax, [rbp+BLK_NEXT]
    test rax, rax
    jz .idle
    mov rcx, [rbp+BLK_USED]          ; USED is final once NEXT is set: look again
    cmp rcx, r13
    ja .blk
    mov rdi, rbp
    mov rbp, rax                     ; read before recycle reuses BLK_NEXT
    xor r13d, r13d
    call recycle
    jmp .iter
.idle:
    test r14d, r14d
    jnz .complete
    call out_flush                   ; nothing more right now: let the lines out
    mov edx, r15d
    lea r10, [poll_ts]               ; lines do not notify, so look again soon
    call ev_sleep
    jmp .iter
.complete:
    test rbp, rbp
    jz .ret
    mov rdi, rbp
    call recycle
.ret:
    ret

; recycle: rdi = a block the writer is done with. Back to its owner's list (only
; the owner takes from it, and takes everything at once, so no ABA), and wake
; one producer that was waiting for room.
recycle:
    mov rax, [rdi+BLK_OWNER]
    shl rax, 6
    lea rcx, [ret_heads]
    add rcx, rax
    mov rax, [rcx]
.push:
    mov [rdi+BLK_NEXT], rax
    lock cmpxchg [rcx], rdi
    jne .push
    lock dec qword [blk_out]
    lock inc dword [bp_seq]
    cmp dword [bp_waiters], 0
    je .ret
    lea rdi, [bp_seq]
    mov esi, FUTEX_WAKE_PRIV
    mov edx, 1
    mov eax, 202
    syscall
.ret:
    ret

; out_put: rdi = bytes, rsi = length. One buffered write() per OUT_SZ, so a
; 1.5M-line search is a few hundred syscalls instead of one per line.
; With --limit it also enforces the cap: count the separators first, and in the
; call that reaches the Nth keep the bytes through it, flush and exit_group.
; shortcut: with newline output N counts output lines, so a newline inside a
;   name counts as a boundary; --null counts entries exactly.
out_put:                                ; rdi = bytes, rsi = length
    ; lim_left is -1 (no cap) or > 0 on entry (--limit 0 exited at startup). It
    ; is 0 only after this call has found the Nth separator.
    push rbx
    push r12
    mov rbx, rdi
    mov r12, rsi
    mov rax, [lim_left]
    cmp rax, -1
    jne .count                          ; a cap is set: look at the separators
.loop:                                  ; the copy loop, unchanged
    test r12, r12
    jz .done
    mov rcx, [out_len]
    mov rax, OUT_SZ
    sub rax, rcx                        ; room left
    cmp r12, rax
    jbe .fits
    mov rdx, rax
    jmp .copy
.fits:
    mov rdx, r12
.copy:
    lea rdi, [out_buf]
    add rdi, rcx
    mov rsi, rbx
    mov rcx, rdx                        ; bytes for this run
    rep movsb
    add [out_len], rdx
    sub r12, rdx
    add rbx, rdx
    cmp qword [out_len], OUT_SZ
    jne .loop
    call out_flush
    jmp .loop
.done:
    cmp qword [lim_left], 0
    je .stop                            ; the cap was reached in this call
    pop r12
    pop rbx
    ret
.stop:
    call out_flush
    xor edi, edi
    mov eax, 231                        ; exit_group: workers die with us
    syscall

.count:                                 ; rax = lim_left, > 0
    mov r10d, 10
    cmp qword [nul_sep], 0
    je .have_sep
    xor r10d, r10d
.have_sep:
    vmovd xmm1, r10d
    vpbroadcastb ymm1, xmm1             ; the separator in every byte lane
    mov rsi, rbx                        ; cursor
    xor r8d, r8d                        ; separators seen
    mov rcx, r12
    shr rcx, 5                          ; whole 32-byte steps
    jz .vec_done
.vec:
    vpcmpeqb ymm0, ymm1, [rsi]          ; unaligned load, inside [rbx, rbx+r12)
    vpmovmskb edx, ymm0
    popcnt edx, edx
    add r8, rdx
    add rsi, 32
    dec rcx
    jnz .vec
.vec_done:
    vzeroupper
    lea rdi, [rbx+r12]                  ; end
.tail:                                  ; fewer than 32 bytes left, branch-free
    cmp rsi, rdi
    jae .counted
    xor edx, edx
    cmp byte [rsi], r10b
    sete dl
    add r8, rdx
    inc rsi
    jmp .tail
.counted:
    cmp r8, rax
    jae .cap
    sub rax, r8                         ; the cap is not reached in this call
    mov [lim_left], rax
    jmp .loop
.cap:                                   ; the rax-th separator ends the output
    xor ecx, ecx                        ; cold: runs once, then the process exits
.find:
    cmp byte [rbx+rcx], r10b
    jne .next_byte
    dec rax
    jz .found
.next_byte:
    inc rcx
    jmp .find
.found:
    lea r12, [rcx+1]                    ; keep the bytes through that separator
    mov qword [lim_left], 0             ; 0 = capped: .done flushes and exits
    jmp .loop

out_flush:
    xor ecx, ecx
.loop:
    cmp rcx, [out_len]
    jae .done
    mov rax, 1                       ; sys_write
    mov edi, 1
    lea rsi, [out_buf]
    add rsi, rcx
    mov rdx, [out_len]
    sub rdx, rcx
    syscall
    test rax, rax
    jle wfail                        ; only a real error; short writes continue
    add rcx, rax
    jmp .loop
.done:
    mov qword [out_len], 0
    ret

; alloc: rdi = size -> rax = arena bytes. Whole chunks are reserved with one
; cmpxchg on g_resv, so threads only meet at chunk granularity. Clobbers rax,
; rcx, rdx, rdi and nothing else: callers keep cursors in r8/r9/rsi across it
; (load_ignore's buffer cursor and walk_dir's name pointer both did, and a
; chunk reservation that clobbered r8 corrupted them).
alloc:
    ; round the request up: tagged item pointers use bit 0, so every block has
    ; to start 8-aligned (the arena base and every chunk are, being page sizes)
    add rdi, 7
    and rdi, -8
    mov rax, [r14+TSTATE.my_top]
    add rax, rdi
    cmp rax, [r14+TSTATE.my_end]
    jbe .have
    mov rdx, rdi
    add rdx, CHUNK
    mov rax, [g_resv]
.retry:
    mov rcx, rax
    add rcx, rdx
    cmp rcx, ARENA_SZ
    jae big
    lock cmpxchg [g_resv], rcx
    jne .retry
    mov rcx, [arena_base]
    add rcx, rax                     ; chunk start
    mov rax, rcx                     ; the block starts there
    mov [r14+TSTATE.my_base], rcx
    add rdx, rcx
    sub rdx, 16                      ; tail guard
    mov [r14+TSTATE.my_end], rdx
    add rcx, rdi                     ; the new top is the chunk start + size,
    jmp .store                       ; not the end of the chunk
.have:
    sub rax, rdi
    lea rcx, [rax+rdi]                ; new top = block start + size
    ; rax = block start, rcx = new top
.store:
    mov [r14+TSTATE.my_top], rcx
    ret

; salloc: rdi = size -> rax = scratch bytes. Stack discipline: walk_dir notes
; sc_top on entry and puts it back on exit, so a directory's names, sort table
; and rules die with its frame. Clobbers rax, rcx, rdi.
salloc:
    add rdi, 7
    and rdi, -8
    mov rax, [r14+TSTATE.sc_top]
    lea rcx, [rax+rdi]
    cmp rcx, [r14+TSTATE.sc_end]
    ja scratch_full
    mov [r14+TSTATE.sc_top], rcx
    ret

; ig_alloc: rdi = size.  Groups can outlive the frame that loaded their rules,
; so every ignore node/rule array is arena-backed now.
ig_alloc:
    jmp alloc

; get_block -> rax = an empty block owned by this thread. Waits while
; BLK_LIMIT blocks are in flight, unless the writer is waiting on our record.
get_block:
    push rbx
.admit:
    mov ebx, [bp_seq]                ; before the checks, see ev_sleep
    cmp qword [blk_out], BLK_LIMIT
    jb .go
    mov rax, [r14+cur_task]
    cmp rax, [emit_cur]
    je .go
    lock inc dword [bp_waiters]
    lea rdi, [bp_seq]
    mov esi, FUTEX_WAIT_PRIV
    mov edx, ebx
    xor r10d, r10d
    mov eax, 202
    syscall
    lock dec dword [bp_waiters]
    jmp .admit
.go:
    lock inc qword [blk_out]
    mov rax, [r14+TSTATE.blk_free]
    test rax, rax
    jnz .pop
    mov rcx, [r14+TSTATE.id]
    shl rcx, 6
    lea rdx, [ret_heads]
    add rdx, rcx
    xor eax, eax
    xchg [rdx], rax                  ; take every block the writer returned
    test rax, rax
    jz .fresh
.pop:
    mov rcx, [rax+BLK_NEXT]
    mov [r14+TSTATE.blk_free], rcx
    jmp .init
.fresh:
    mov rdi, BLK_SZ
    call alloc
    mov rcx, [r14+TSTATE.id]
    mov [rax+BLK_OWNER], rcx
.init:
    mov qword [rax+BLK_NEXT], 0
    mov qword [rax+BLK_USED], 0
    pop rbx
    ret

; tolower the byte in al (ASCII only)
%macro TOLOWER 1
    cmp %1, 'A'
    jb %%done
    cmp %1, 'Z'
    ja %%done
    or %1, 0x20
%%done:
%endmacro

; name_match: rdi = name, rsi = pattern, rdx = pattern length -> al
; literal substring search, two loops picked once by pat_ci. The folded one
; folds both bytes: pat_ci=1 means fold both bytes. In smart mode,
; folding a pattern byte is the identity.
%macro MATCH_BODY 1
%%outer:
    cmp byte [rbx], 0
    je %%no
    mov rdi, rbx
    mov rsi, r13
    mov rcx, r14
%%inner:
    mov dl, [rdi]
    test dl, dl
    jz %%next                        ; name ended inside the pattern
    mov al, [rsi]
%if %1
    TOLOWER dl
    TOLOWER al
%endif
    cmp al, dl
    jne %%next
    inc rdi
    inc rsi
    dec rcx
    jnz %%inner
    mov eax, 1
    jmp .ret
%%next:
    inc rbx
    jmp %%outer
%%no:
    xor eax, eax
    jmp .ret
%endmacro
name_match:
    push rbx
    push r12
    push r13
    push r14
    mov rbx, rdi                     ; current start in the name
    mov r13, rsi
    mov r14, rdx
    test r14, r14
    jz .yes                          ; an empty pattern matches every entry
    cmp qword [pat_ci], 0
    jne .ci
    MATCH_BODY 0
.ci:
    MATCH_BODY 1
.yes:
    mov eax, 1
.ret:
    pop r14
    pop r13
    pop r12
    pop rbx
    ret

; The table of a directory holds 16-byte entries {key, name pointer}; two
; metadata bytes precede each pointed-to name. The key is
; the first 8 bytes of the name, byte-swapped into a big-endian integer and
; zero-padded after the NUL, so comparing keys as unsigned integers orders names
; bytewise without touching memory; only names that share their first 8 bytes
; need the names themselves (tie_cmp).

; tie_cmp: rdi, rsi = two names with equal keys -> eax = -1 / 0 / 1 (unsigned
; bytes). Compares 8 bytes at a time from offset 8 and only looks at single
; bytes once a word differs. Distinct names in one directory differ at or
; before the NUL of the shorter one, so bytes past a NUL never decide.
; Reads up to 7 bytes past a name: scratch regions keep a 16-byte tail guard.
; Clobbers rax, rcx, rdx, xmm0, xmm1.
tie_cmp:
    mov ecx, 8
.word:
    mov rax, [rdi+rcx]
    mov rdx, [rsi+rcx]
    cmp rax, rdx
    jne .diff
    movq xmm0, rax
    pxor xmm1, xmm1
    pcmpeqb xmm0, xmm1
    pmovmskb edx, xmm0
    test dl, dl
    jnz .same                        ; equal words holding the NUL: same name
    add ecx, 8
    jmp .word
.same:
    xor eax, eax
    ret
.diff:
    xor rax, rdx
    bsf rax, rax
    shr eax, 3                       ; first differing byte (memory order)
    add rax, rcx
    movzx ecx, byte [rdi+rax]
    movzx edx, byte [rsi+rax]
    cmp ecx, edx
    ja .gt
    mov eax, -1
    ret
.gt:
    mov eax, 1
    ret

; isort: insertion-sort the entries in [r8, r9). Clobbers rax, rcx, rdx, rsi,
; rdi, r10, r11, rbp, r12, xmm0, xmm1.
isort:
    lea r10, [r8+16]                 ; next entry to insert
.next:
    cmp r10, r9
    jae .done
    mov r11, [r10]                   ; its key
    mov rbp, [r10+8]                 ; its name
    mov r12, r10                     ; the hole
.shift:
    cmp r12, r8
    jbe .place
    mov rax, [r12-16]
    cmp rax, r11
    jb .place                        ; previous key is smaller
    ja .move
    mov rdi, [r12-8]                 ; equal keys: the names decide
    mov rsi, rbp
    call tie_cmp
    test eax, eax
    jle .place
.move:
    mov rax, [r12-16]
    mov [r12], rax
    mov rax, [r12-8]
    mov [r12+8], rax
    sub r12, 16
    jmp .shift
.place:
    mov [r12], r11
    mov [r12+8], rbp
    add r10, 16
    jmp .next
.done:
    ret

; merge: left run [r8, r9) and right run [r10, r11) -> ascending at rbp.
; Clobbers rax, rcx, rdx, rsi, rdi, r8, r10, rbp, xmm0, xmm1.
merge:
.cmp:
    cmp r8, r9
    jae .right_rest
    cmp r10, r11
    jae .left_rest
    mov rax, [r8]
    cmp rax, [r10]
    jb .left
    ja .right
    mov rdi, [r8+8]
    mov rsi, [r10+8]
    call tie_cmp
    test eax, eax
    jg .right
.left:
    mov rax, [r8]
    mov [rbp], rax
    mov rax, [r8+8]
    mov [rbp+8], rax
    add r8, 16
    add rbp, 16
    jmp .cmp
.right:
    mov rax, [r10]
    mov [rbp], rax
    mov rax, [r10+8]
    mov [rbp+8], rax
    add r10, 16
    add rbp, 16
    jmp .cmp
.right_rest:                         ; left is used up: the right tail goes as is
    mov rsi, r10
    mov rcx, r11
    sub rcx, r10
    jmp .copy
.left_rest:
    mov rsi, r8
    mov rcx, r9
    sub rcx, r8
.copy:
    mov rdi, rbp
    rep movsb
    ret

; sort_table: sort the rcx entries at r15 by name, bytewise. Up to SORT_RUN
; entries (nearly every directory) go through insertion sort; above that,
; insertion-sorted runs are merged bottom-up through a temporary buffer in
; scratch that is released again before returning. O(n log n), and mostly
; integer compares of cached keys. Preserves rbx, r12-r14; clobbers the rest.
sort_table:
    cmp rcx, 2
    jb .quick
    push rbx
    push rbp
    push r12
    push r13
    sub rsp, 48                      ; [0]=bytes [8]=table [16]=temp [24]=src
    mov rax, rcx                     ; [32]=scratch mark [40]=dst
    shl rax, 4
    mov [rsp], rax
    mov [rsp+8], r15
    cmp rcx, SORT_RUN
    ja .big
    mov r8, r15
    lea r9, [r15+rax]
    call isort
    jmp .out
.big:
    mov rax, [r14+TSTATE.sc_top]
    mov [rsp+32], rax
    mov rdi, [rsp]
    call salloc
    mov [rsp+16], rax
    mov r13, [rsp+8]                 ; 1. insertion-sort each run in place
.runs:
    mov rax, [rsp+8]
    add rax, [rsp]
    cmp r13, rax
    jae .runs_done
    mov r8, r13
    lea r9, [r13+SORT_RUN*16]
    cmp r9, rax
    jbe .run_end
    mov r9, rax
.run_end:
    call isort
    add r13, SORT_RUN*16
    jmp .runs
.runs_done:
    mov rax, [rsp+8]
    mov [rsp+24], rax                ; 2. merge passes: src -> dst, then swap
    mov r13, SORT_RUN*16             ; width of a sorted run, in bytes
.pass:
    cmp r13, [rsp]
    jae .passes_done
    mov rax, [rsp+24]
    mov rdx, [rsp+16]
    cmp rax, [rsp+8]
    je .have_dst
    mov rdx, [rsp+8]                 ; src is the temp buffer: write back
.have_dst:
    mov [rsp+40], rdx
    mov rbx, rax                     ; src cursor
    mov r12, rdx                     ; dst cursor
    mov r15, rax
    add r15, [rsp]                   ; src end
.pair:
    cmp rbx, r15
    jae .pass_done
    mov r8, rbx
    lea r9, [rbx+r13]
    cmp r9, r15
    jbe .left_ok
    mov r9, r15
.left_ok:
    mov r10, r9                      ; right run follows the left one
    lea r11, [r9+r13]
    cmp r11, r15
    jbe .right_ok
    mov r11, r15
.right_ok:
    mov rbp, r12
    call merge
    lea rax, [r13+r13]
    add rbx, rax
    add r12, rax
    jmp .pair
.pass_done:
    mov rax, [rsp+40]
    mov [rsp+24], rax
    add r13, r13
    jmp .pass
.passes_done:
    mov rsi, [rsp+24]
    mov rdi, [rsp+8]
    cmp rsi, rdi
    je .in_place
    mov rcx, [rsp]
    rep movsb                        ; the last pass ended in the temp buffer
.in_place:
    mov rax, [rsp+32]
    mov [r14+TSTATE.sc_top], rax     ; give the temp buffer back
.out:
    add rsp, 48
    pop r13
    pop r12
    pop rbp
    pop rbx
.quick:
    ret

; print_entry: rdi = path length, rsi = is dir. Appends "path[/]\n" straight into
; the current record's last block (a new block when the line does not fit) and
; publishes it by bumping BLK_USED; the writer may be reading that block now.
; A group's walk opens a text record on its first line; the record is linked
; into the group before its first block is requested.  With --null the separator is
; a NUL instead of the newline.
print_entry:
    push rbx
    push r12
    push r13
    mov rbx, rdi                     ; path length
    mov r13, rsi                     ; is_dir
    lea r12, [rdi+rsi+1]             ; line length, separator included
    cmp qword [r14+cur_task], 0
    jne .rec
    call new_text_record
.rec:
    mov rdx, [r14+cur_blk]
    test rdx, rdx
    jz .newblk
    mov rcx, [rdx+BLK_USED]
    add rcx, r12
    cmp rcx, BLK_DATA
    jbe .room
.newblk:
    call get_block
    mov rdx, [r14+cur_blk]
    test rdx, rdx
    jz .first
    mov [rdx+BLK_NEXT], rax          ; the full block's USED is final: link
    jmp .linked
.first:
    mov rcx, [r14+cur_task]
    mov [rcx+REC_HEAD], rax
.linked:
    mov [r14+cur_blk], rax
    mov rdx, rax
.room:
    mov rdi, [rdx+BLK_USED]
    lea rdi, [rdx+rdi+BLK_HDR]       ; where the line goes
    lea rsi, [r14+path_buf]
    mov rcx, rbx
    rep movsb
    test r13, r13
    jz .nl
    mov byte [rdi], '/'
    inc rdi
.nl:
    mov al, 10                       ; '\n'
    cmp qword [nul_sep], 0
    je .sep
    xor eax, eax                     ; --null: NUL, so paths with newlines survive
.sep:
    mov byte [rdi], al
    add [rdx+BLK_USED], r12          ; publish
    pop r13
    pop r12
    pop rbx
    ret

; append_item: rdi = owning group, rsi = item, edx = 1 for a GRP, 0 for a REC.
; Publishes one link at the end of the group's item list.  The item's NEXT is
; cleared before the link becomes visible.
append_item:
    mov r8, rsi
    or r8, rdx                       ; tagged item
    test edx, edx
    jz .rec_item
    mov qword [rsi+GRP_ITEM_NEXT], 0
    jmp .next_ready
.rec_item:
    mov qword [rsi+REC_NEXT], 0
.next_ready:
    mov rax, [rdi+GRP_LAST]
    test rax, rax
    jz .first
    mov rcx, rax
    and rcx, -2
    test al, 1
    jz .old_rec
    mov [rcx+GRP_ITEM_NEXT], r8
    jmp .last
.old_rec:
    mov [rcx+REC_NEXT], r8
    jmp .last
.first:
    mov [rdi+GRP_FIRST], r8
.last:
    mov [rdi+GRP_LAST], r8
    jmp notify_ev

; new_text_record: the walking thread starts a record for its own lines
new_text_record:
    push rbx
    mov rdi, REC_SZ
    call alloc
    mov rbx, rax
    mov qword [rbx+REC_HEAD], 0
    mov dword [rbx+REC_DONE], 0
    mov rdi, [r14+cur_grp]
    mov rsi, rbx
    xor edx, edx
    call append_item
    mov [r14+cur_task], rbx
    mov qword [r14+cur_blk], 0
    pop rbx
    ret


die:                                 ; rsi, rdx = message; exits 1
    mov rdi, 2
    mov rax, 1
    syscall
    mov rax, 231                     ; exit_group: the workers die with us
    mov rdi, 1
    syscall

wfail:
    lea rsi, [e_write]
    mov rdx, e_write_e - e_write
    jmp die

bad_args:
    lea rsi, [e_arg]
    mov rdx, e_arg_e - e_arg
    jmp die

bad_limit:
    lea rsi, [e_limit]
    mov rdx, e_limit_e - e_limit
    jmp die

bad_type:
    lea rsi, [e_type]
    mov rdx, e_type_e - e_type
    jmp die

bad_flag:
    lea rsi, [e_flag]
    mov rdx, e_flag_e - e_flag
    jmp die

bad_case_comb:
    lea rsi, [e_case]
    mov rdx, e_case_e - e_case
    jmp die

bad_hid_comb:
    lea rsi, [e_hid]
    mov rdx, e_hid_e - e_hid
    jmp die

bad_ig_comb:
    lea rsi, [e_ig]
    mov rdx, e_ig_e - e_ig
    jmp die

big:
    lea rsi, [e_big]
    mov rdx, e_big_e - e_big
    jmp die

scratch_full:
    lea rsi, [e_scratch]
    mov rdx, e_scratch_e - e_scratch
    jmp die


; globmatch: rdi = pattern, rsi = string (both NUL-terminated) -> al = 1 on a
; whole-string match. gitignore syntax: '*' stops at '/', "**/" spans any
; number of path components, "/**" at the end spans everything below.
; Case flags (--case-sensitive / --case-insensitive) apply only to the search pattern;
; .gitignore pattern matching is always exact (case-sensitive).
globmatch:
    push rbx
    push r12
    push r13
    push r14
    mov rbx, rdi
    mov r12, rdi                     ; pattern start
    mov r13, rsi
.loop:
    mov al, [rbx]
    test al, al
    jz .end
    cmp al, '*'
    je .star
    cmp al, '?'
    je .qmark
    cmp al, [r13]
    jne .fail
    inc rbx
    inc r13
    jmp .loop
.end:
    cmp byte [r13], 0
    sete al
    jmp .ret
.qmark:
    mov al, [r13]
    test al, al
    jz .fail
    cmp al, '/'
    je .fail
    inc rbx
    inc r13
    jmp .loop
.star:
    mov r14, rbx                     ; first star of the run
    xor ecx, ecx
.scan_star:
    inc rbx
    cmp byte [rbx], '*'
    jne .stars_done
    mov ecx, 1
    jmp .scan_star
.stars_done:
    test ecx, ecx
    jz .plain
    cmp byte [rbx], '/'
    je .starstar_slash
    cmp byte [rbx], 0
    jne .plain                       ; "**x": git reads it as a plain '*'
    cmp r14, r12
    je .plain
    cmp byte [r14-1], '/'
    jne .plain
    mov eax, 1                       ; trailing "/**": everything below
    jmp .ret
.starstar_slash:                     ; "**/" = zero or more components
.ssl:
    lea rdi, [rbx+1]
    mov rsi, r13
    call globmatch
    test al, al
    jnz .succ
.ssl_skip:
    mov al, [r13]
    test al, al
    jz .fail
    inc r13
    cmp al, '/'
    jne .ssl_skip
    jmp .ssl
.plain:                              ; single '*', does not cross '/'
.ps:
    mov rdi, rbx
    mov rsi, r13
    call globmatch
    test al, al
    jnz .succ
    mov al, [r13]
    test al, al
    jz .fail
    cmp al, '/'
    je .fail
    inc r13
    jmp .ps
.succ:
    mov eax, 1
    jmp .ret
.fail:
    xor eax, eax
.ret:
    pop r14
    pop r13
    pop r12
    pop rbx
    ret

; ig_ignored: rdi = entry name, rsi = entry path, rdx = is dir -> al
; ig_chain links one immutable node per directory that has rules, innermost
; last. Ancestors are applied before the node itself, so the deepest matching
; rule still wins - the order the single-threaded walk appended them in.
ig_ignored:
    push rbx
    push rbp
    push r12
    push r13
    push r15
    mov rbx, rdi
    mov rbp, rsi
    mov r13, rdx
    xor r12d, r12d                   ; result so far
    mov r15, [r14+ig_chain]
    call ig_levels
    mov eax, r12d
    pop r15
    pop r13
    pop r12
    pop rbp
    pop rbx
    ret

; ig_levels: apply r15's rules, then its ancestors'
ig_levels:
    mov rax, [r15+NODE_PARENT]
    test rax, rax
    jz .self
    push r15                         ; our own node, back after the recursion
    mov r15, rax                     ; ancestors first: the deepest match wins
    call ig_levels
    pop r15
.self:
    mov r8, [r15+NODE_RULES]
    mov rcx, [r15+NODE_NRULES]
    xor r9d, r9d
.rule:
    cmp r9, rcx
    jae .ret
    mov rax, r9
    shl rax, 5
    add rax, r8
    push rax
    mov rdx, [rax+8]                 ; flags
    test rdx, F_DIR
    jz .ok
    test r13, r13
    jnz .ok
    pop rax                          ; a dir rule cannot match a non-directory
    jmp .skip
.ok:
    mov rdi, [rax]
    test rdx, F_ANCHOR
    jz .unanchored
    mov rdx, [rax+16]                ; rule's directory prefix length
    lea rsi, [rbp+rdx]               ; match the path below that directory
    jmp .match
.unanchored:
    mov rsi, rbx                     ; match the entry's name
.match:
    call globmatch
    pop rdx                          ; the rule record; al stays the result
    mov rcx, [r15+NODE_NRULES]        ; globmatch reuses rcx
    test al, al
    jz .skip
    mov rdx, [rdx+8]
    test rdx, F_NEG
    jnz .unignore
    mov r12d, 1
    jmp .skip
.unignore:
    xor r12d, r12d
.skip:
    inc r9
    jmp .rule
.ret:
    ret

; ig_add_line: rdi = one .gitignore line, NUL-terminated; appends a rule to
; this thread's scratch array. rbx (the containing directory's path length)
; is used for the prefix.
ig_add_line:
    mov al, [rdi]
    test al, al
    jz .ret                          ; blank line
    cmp al, '#'
    je .ret
    mov rsi, rdi
.find_end:
    cmp byte [rsi], 0
    je .trim
    inc rsi
    jmp .find_end
.trim:
    cmp rsi, rdi
    je .ret                          ; whitespace only
    mov al, [rsi-1]
    cmp al, ' '
    je .trim_one
    cmp al, 9
    jne .trimmed
.trim_one:
    dec rsi
    jmp .trim
.trimmed:
    mov byte [rsi], 0
    xor r11d, r11d                   ; flags
    cmp byte [rdi], '!'
    jne .no_neg
    or r11d, F_NEG
    inc rdi
    cmp rdi, rsi
    jae .ret                         ; a lone "!"
.no_neg:
    cmp byte [rsi-1], '/'
    jne .no_dir
    or r11d, F_DIR
    dec rsi
    mov byte [rsi], 0
    cmp rdi, rsi
    jae .ret
.no_dir:
    cmp byte [rdi], '/'
    jne .scan_slash
    or r11d, F_ANCHOR
    inc rdi
.scan_slash:                         ; any other '/' anchors it too
    mov rcx, rdi
.sl:
    cmp rcx, rsi
    jae .store
    cmp byte [rcx], '/'
    je .anchored
    inc rcx
    jmp .sl
.anchored:
    or r11d, F_ANCHOR
.store:
    cmp rdi, rsi
    jae .ret
    mov rax, [r14+ig_nrules]
    cmp rax, IG_MAX
    jae .ret                         ; rule table full: drop the rest
    ; copy the pattern out of igbuf, which this thread reuses for the next
    ; directory; rules handed to another thread (as a chain node) outlive it,
    ; hence ig_alloc
    push r12
    mov r12, rdi                     ; pattern start
    mov rcx, rsi
    sub rcx, rdi
    inc rcx                          ; length, NUL included
    push rcx
    mov rdi, rcx
    call ig_alloc
    pop rcx
    mov r10, rax                     ; the copy
    mov rdi, rax
    mov rsi, r12
    rep movsb
    pop r12
    mov rdi, r10
    mov rax, [r14+ig_nrules]
    mov rcx, rax
    shl rcx, 5
    lea rdx, [r14+ig_rules]
    add rdx, rcx
    mov [rdx], rdi
    mov [rdx+8], r11                 ; flags
    mov rcx, rbx                     ; prefix includes the trailing '/'
    test rcx, rcx
    jz .base
    inc rcx
.base:
    mov [rdx+16], rcx
    inc qword [r14+ig_nrules]
.ret:
    ret

; load_ignore: read this directory's .gitignore (if this is a repo) into
; igbuf and append its rules; rbx = directory path length.
; If it added any, publish a chain node holding a copy of the whole active
; rule stack, so child groups created below it keep their ancestors' rules.
load_ignore:
    push rbx
    push r12
    push r13
    sub rsp, 32
    cmp qword [git_repo], 0
    je .ret
    mov [rsp], rbx                   ; ig_add_line wants the prefix length
    mov rbx, [r14+ig_nrules]
    mov [rsp+16], rbx                ; rules before this file
    mov rbx, [rsp]                   ; the directory's path length
    lea rdi, [r14+path_buf]
    add rdi, rbx
    test rbx, rbx
    jz .nols
    mov byte [rdi], '/'
    inc rdi
.nols:
    lea rsi, [igname]
.cp:
    mov al, [rsi]
    mov [rdi], al
    inc rdi
    inc rsi
    test al, al
    jnz .cp
    mov rax, 257                     ; openat
    mov rdi, AT_FDCWD
    lea rsi, [r14+path_buf]
    xor edx, edx
    xor r10d, r10d
    syscall
    test rax, rax
    js .ret
    mov r8, rax                      ; fd
    lea rsi, [r14+igbuf]
    xor r10d, r10d                   ; bytes read
.read:
    mov rax, 0                       ; sys_read
    mov rdi, r8
    mov rdx, IGBUF_SZ - 1
    sub rdx, r10
    syscall
    add r10, rax
    add rsi, rax
    cmp rax, 0
    jle .read_done
    cmp r10, IGBUF_SZ - 1
    jb .read
.read_done:
    mov rax, 3                       ; close
    mov rdi, r8
    syscall
    test r10, r10
    jz .ret
    lea r8, [r14+igbuf]
    lea r9, [r8+r10]                 ; end of content
.line:
    cmp r8, r9
    jae .publish
    mov rsi, r8
.nl:
    cmp rsi, r9
    jae .eof
    cmp byte [rsi], 10
    je .found
    inc rsi
    jmp .nl
.found:
    mov byte [rsi], 0
    inc rsi
    mov rdi, r8
    mov r8, rsi
    mov rbx, [rsp]
    call ig_add_line
    jmp .line
.eof:
    mov byte [r9], 0                 ; buffer keeps a byte for this NUL
    mov rdi, r8
    mov rbx, [rsp]
    call ig_add_line
    jmp .publish                     ; that was the last line: r8 cannot advance
.publish:
    mov rax, [r14+ig_nrules]
    cmp rax, [rsp+16]
    je .ret                          ; no new rules: the chain is unchanged
    mov rdi, NODE_SZ
    call ig_alloc
    mov rbx, rax                     ; the node
    mov rcx, [r14+ig_nrules]
    sub rcx, [rsp+16]                ; only this directory's own rules
    mov [rbx+NODE_NRULES], rcx
    mov rcx, [r14+ig_chain]
    mov [rbx+NODE_PARENT], rcx
    mov [r14+ig_chain], rbx
    mov rcx, [r14+ig_nrules]
    sub rcx, [rsp+16]
    shl rcx, 5
    push rcx
    mov rdi, rcx
    call ig_alloc
    pop rcx
    mov [rbx+NODE_RULES], rax
    mov rdi, rax
    lea rsi, [r14+ig_rules]
    mov rdx, [rsp+16]
    shl rdx, 5
    add rsi, rdx                     ; this level's first rule
    rep movsb
    mov rax, [rsp+16]                ; rules are copied into the node: free the
    mov [r14+ig_nrules], rax         ; scratch slots, so IG_MAX is per directory
.ret:
    add rsp, 32
    pop r13
    pop r12
    pop rbx
    ret

; arena_put: append [d_type][matched][name,0] with a three-byte header, using
; packed metadata in rdi.
; rdi = d_type | (matched << 8), r15 = d_name -> rax = record, rdx = sort key
; (first 8 name bytes, big-endian, zero after the NUL). The length pass could be
; avoided by allocating d_reclen, but that wastes ~27 bytes per entry (~80 MB on
; /nix/store). Clobbers rax, rcx, rdx, rsi, rdi - not r8-r11/r14/r15, which the
; directory loop is holding.
arena_put:
    push rbx
    push r12
    push r13
    mov rbx, r15
    mov r12, rdi                    ; packed metadata
    xor r13d, r13d
.len:
    cmp byte [rbx+r13], 0
    je .lend
    inc r13
    jmp .len
.lend:
    lea rdi, [r13+3]
    call salloc
    mov [rax], r12w
    lea rdi, [rax+2]
    mov rsi, rbx
    lea rcx, [r13+1]
    rep movsb
    mov rdx, [rax+2]                ; the key, from the copy (a guard follows it)
    cmp r13, 8
    jae .whole
    mov ecx, r13d
    shl ecx, 3
    mov esi, 1
    shl rsi, cl
    dec rsi
    and rdx, rsi                    ; keep only the name's own bytes
.whole:
    bswap rdx
    pop r13
    pop r12
    pop rbx
    ret

; grow_table: double the current directory's pointer table into fresh scratch.
; The old copy is garbage until walk_dir's frame ends and gives all of it back.
grow_table:
    push rbx
    mov rbx, [r14+TSTATE.tab_ptr]
    mov rax, [r14+TSTATE.tab_cap]
    test rax, rax
    jz .capped
    shl rax, 1                       ; otherwise double
    jmp .store
.capped:
    mov rax, 256                     ; first grow: no table yet
.store:
    mov [r14+TSTATE.tab_cap], rax
    shl rax, 4                       ; 16-byte entries
    mov rdi, rax
    call salloc
    mov [r14+TSTATE.tab_ptr], rax
    mov rcx, [r14+TSTATE.tab_n]
    shl rcx, 4
    mov rdi, rax
    mov rsi, rbx                     ; the old array, absent on the first grow
    rep movsb
    pop rbx
    ret

; try_range_split: rdi = sorted {key,name} table, rsi = N.  If the directory is
; large enough, copy contiguous slices of the table and its name records into
; arena-backed range groups.  Each range owns its slice, so there is no shared
; slab and no refcount.  All ranges are pushed first, then linked in order, so
; the first range can be claimed by the owner while workers take the rest.
; Returns eax=1 after publishing the groups.
try_range_split:
    push rbx
    push rbp
    push r12
    push r13
    push r15
    sub rsp, 128
    mov [rsp+0], rdi                 ; source table
    mov [rsp+8], rsi                 ; N
    mov rax, rsi
    add rax, CHUNK_MIN - 1
    xor edx, edx
    mov rcx, CHUNK_MIN
    div rcx
    cmp rax, MAX_RANGE_CHUNKS
    jbe .k_ok
    mov eax, MAX_RANGE_CHUNKS
.k_ok:
    cmp rax, 2
    jb .no
    mov [rsp+16], rax                ; K
    mov rdi, rax
    shl rdi, 3
    call salloc
    mov [rsp+96], rax                ; group pointer array
    xor r12d, r12d                   ; range index
.range_loop:
    cmp r12, [rsp+16]
    jae .yes
    mov rax, r12
    imul rax, [rsp+8]
    xor edx, edx
    div qword [rsp+16]
    mov [rsp+24], rax                ; lo
    lea rax, [r12+1]
    imul rax, [rsp+8]
    xor edx, edx
    div qword [rsp+16]
    mov [rsp+32], rax                ; hi
    sub rax, [rsp+24]
    mov [rsp+40], rax                ; count

    ; measure and copy this slice's names (metadata + NUL included)
    xor r13d, r13d
    mov rbx, [rsp+24]
.name_scan:
    cmp rbx, [rsp+32]
    jae .name_scanned
    mov rax, rbx
    shl rax, 4
    add rax, [rsp+0]
    mov rdi, [rax+8]
    call strlen
    add rax, 3
    add r13, rax
    inc rbx
    jmp .name_scan
.name_scanned:
    mov [rsp+48], r13                ; name bytes
    mov rdi, GRP_SZ
    call alloc
    mov [rsp+56], rax                ; group
    mov rdi, [rsp+40]
    shl rdi, 4
    call alloc
    mov [rsp+64], rax                ; table copy
    mov rdi, [rsp+48]
    call alloc
    mov [rsp+72], rax                ; names copy
    mov rax, [r14+path_len]
    mov [rsp+80], rax
    lea rdi, [rax+1]
    call alloc
    mov [rsp+88], rax                ; path copy
    mov rcx, [rsp+80]
    lea rsi, [r14+path_buf]
    mov rdi, rax
    rep movsb
    mov byte [rdi], 0

    mov rbx, [rsp+24]                ; source index
    mov r13, [rsp+72]                ; names cursor
    mov rbp, [rsp+64]                ; table cursor
.copy_loop:
    cmp rbx, [rsp+32]
    jae .copy_done
    mov rax, rbx
    shl rax, 4
    add rax, [rsp+0]
    mov rdi, [rax+8]                 ; source name
    call strlen
    mov r15, rax
    add r15, 3                       ; metadata + name + NUL
    mov rax, rbx
    shl rax, 4
    add rax, [rsp+0]
    mov rsi, [rax+8]
    sub rsi, 2
    mov rdi, r13
    mov rcx, r15
    rep movsb
    lea rax, [r13+2]                 ; new name record
    mov [rbp+0], rax
    mov [rbp+8], rax
    add r13, r15
    add rbp, 16
    inc rbx
    jmp .copy_loop
.copy_done:
    mov rbx, [rsp+56]
    mov qword [rbx+GRP_WORK_NEXT], 0
    mov qword [rbx+GRP_ITEM_NEXT], 0
    mov dword [rbx+GRP_STATE], GRP_NEW
    mov dword [rbx+GRP_KIND], GRP_RANGE
    mov qword [rbx+GRP_FIRST], 0
    mov qword [rbx+GRP_LAST], 0
    mov rax, [rsp+88]
    mov [rbx+GRP_PATH], rax
    mov rax, [rsp+80]
    mov [rbx+GRP_PATHLEN], rax
    mov rax, [r14+ig_chain]
    mov [rbx+GRP_CHAIN], rax
    mov rax, [rsp+64]
    mov [rbx+GRP_TAB], rax
    mov rax, [rsp+72]
    mov [rbx+GRP_NAMES], rax
    mov rax, [rsp+40]
    mov [rbx+GRP_N], rax
    mov rdi, rbx
    call push_group
    mov rcx, [rsp+96]
    mov [rcx+r12*8], rbx
    inc r12
    jmp .range_loop
.yes:
    ; Every range is already on the work stack.  Publish them in output order;
    ; link_group claims the first one itself if no worker beat it.
    xor r12d, r12d
.publish:
    cmp r12, [rsp+16]
    jae .published
    mov rcx, [rsp+96]
    mov rdi, [rcx+r12*8]
    call link_group
    inc r12
    jmp .publish
.published:
    mov eax, 1
    jmp .ret
.no:
    xor eax, eax
.ret:
    add rsp, 128
    pop r15
    pop r13
    pop r12
    pop rbp
    pop rbx
    ret

; walk_wide: rdi = sorted table, rsi = N.  Prepass resolves types and filters,
; creating and pushing a group for every non-ignored directory before any of
; them is linked.  Publish then walks the table in order, prints the matching
; lines and links each child; a child still NEW is claimed and executed inline.
; Because every sibling is already on the work stack, that inline execution
; cannot serialise the level and cannot deadlock the writer.
walk_wide:
    push rbx
    push rbp
    push r12
    push r13
    push r15
    sub rsp, 96
    mov [rsp+0], rdi                 ; table
    mov [rsp+8], rsi                 ; N
    mov rax, [r14+TSTATE.sc_top]
    mov [rsp+16], rax                ; scratch mark
    mov rdi, rsi
    call salloc
    mov [rsp+24], rax                ; flags
    mov rdi, rax
    xor eax, eax
    mov rcx, [rsp+8]
    rep stosb
    mov rdi, [rsp+8]
    shl rdi, 3
    call salloc
    mov [rsp+32], rax                ; child groups
    mov rdi, rax
    xor eax, eax
    mov rcx, [rsp+8]
    shl rcx, 3
    rep stosb
    mov rbx, [r14+path_len]
    xor r15d, r15d
.prepass:
    cmp r15, [rsp+8]
    jae .publish
    mov rax, r15
    shl rax, 4
    add rax, [rsp+0]
    mov r12, [rax+8]
    call walk_dir.mkpath
    movzx r13d, byte [r12-2]
    mov [rsp+40], r13
    test r13b, r13b
    jnz .have_type
    mov rax, 262                     ; DT_UNKNOWN: lstat without following
    mov rdi, AT_FDCWD
    lea rsi, [r14+path_buf]
    lea rdx, [r14+statbuf]
    mov r10, 0x100
    syscall
    test rax, rax
    js .type_unknown
    mov eax, [r14+statbuf+24]
    and eax, 0xF000
    cmp eax, 0x4000
    je .type_dir
    cmp eax, 0x8000
    je .type_file
.type_unknown:
    mov byte [r12-2], 0
    jmp .have_type
.type_dir:
    mov byte [r12-2], 4
    jmp .have_type
.type_file:
    mov byte [r12-2], 8
.have_type:
    movzx r13d, byte [r12-2]
    mov [rsp+40], r13
    mov rax, [r14+ig_chain]
    test rax, rax
    jz .no_ignore
    mov rdi, r12
    lea rsi, [r14+path_buf]
    xor edx, edx
    cmp r13b, 4
    sete dl
    call ig_ignored
    test al, al
    jnz .ignored
.no_ignore:
    xor r13d, r13d                   ; match flag
    test byte [r12-1], 1
    jnz .matched
    mov rdx, [pat_len]
    test rdx, rdx
    jz .type_gate
    mov rdi, r12
    mov rsi, [pat_ptr]
    call name_match
    test al, al
    jz .match_done
.type_gate:
    cmp byte [type_seen], 0
    je .matched
    movzx ecx, byte [rsp+40]
    bt qword [res_type], rcx
    jnc .match_done
.matched:
    mov r13d, 2
.match_done:
    mov rax, [rsp+24]
    mov [rax+r15], r13b
    cmp byte [rsp+40], 4
    jne .prep_next
    mov rdi, rbp
    call make_group
    mov rcx, [rsp+32]
    mov [rcx+r15*8], rax
    jmp .prep_next
.ignored:
    mov rax, [rsp+24]
    mov byte [rax+r15], 1
.prep_next:
    inc r15
    jmp .prepass
.publish:
    xor r15d, r15d
.pub_loop:
    cmp r15, [rsp+8]
    jae .wide_done
    mov rax, [rsp+24]
    movzx ecx, byte [rax+r15]
    test cl, 1
    jnz .pub_next
    test cl, 2
    jz .pub_link
    mov rax, r15
    shl rax, 4
    add rax, [rsp+0]
    mov r12, [rax+8]
    call walk_dir.mkpath
    mov rdi, rbp
    xor esi, esi
    cmp byte [r12-2], 4
    sete sil
    call print_entry
.pub_link:
    mov rax, [rsp+32]
    mov rdi, [rax+r15*8]
    test rdi, rdi
    jz .pub_next
    call link_group
.pub_next:
    inc r15
    jmp .pub_loop
.wide_done:
    mov rax, [rsp+16]
    mov [r14+TSTATE.sc_top], rax
    add rsp, 96
    pop r15
    pop r13
    pop r12
    pop rbp
    pop rbx
    ret

; walk_dir: search the directory at path_buf[0..path_len) (empty = cwd).
; Entries are collected first, sorted by name, then printed and recursed in
; that order. Collecting applies pattern and type prefilters: directories and
; unknown entries stay for traversal/fallback; known non-directories that cannot
; print are dropped, so with a selective pattern the
; sort and the scratch hold a handful of entries instead of the whole
; directory. Sorting is never skipped, whatever the directory size.
; Everything the frame allocates in scratch (names, table, and rules) is
; released when it returns: [rsp+48] holds the mark.
walk_dir:
    push rbx
    push rbp
    push r12
    push r13
    push r14
    push r15
    sub rsp, 64
    ; [rsp]=saved ig_nrules [rsp+8]=saved chain [rsp+16]=count
    ; [rsp+24]=sort table [rsp+32]=split [rsp+40]=dir fd (-1: none/closed)
    ; [rsp+48]=scratch mark
    ; [rsp+56]=while parsing: this directory lists a .gitignore (as git_repo:
    ;          so 0 unless --gitignore was given and cwd contains .git); while iterating:
    ;          the current entry's resolved type
    mov rax, [r14+ig_nrules]
    mov [rsp], rax
    mov rax, [r14+ig_chain]
    mov [rsp+8], rax
    mov qword [rsp+16], 0
    mov qword [rsp+24], 0
    mov qword [rsp+32], 0
    mov qword [r14+TSTATE.tab_n], 0
    ; a nested walk_dir is still iterating its own table: each frame starts a
    ; fresh one, in scratch above everything the frames below it hold
    mov qword [r14+TSTATE.tab_ptr], 0
    mov qword [r14+TSTATE.tab_cap], 0
    mov rax, [r14+TSTATE.sc_top]
    mov [rsp+48], rax                ; everything this frame allocates dies at .fin
    mov qword [rsp+56], 0            ; no .gitignore seen yet
    mov qword [rsp+40], -1           ; no directory open yet
    mov rbx, [r14+path_len]
    mov r12, [r14+f_deep]
    inc qword [r14+f_deep]
    lea rdi, [dot]
    test rbx, rbx
    jz .open
    lea rdi, [r14+path_buf]
.open:
    mov rsi, rdi                     ; the path, from the current directory
    mov rdi, [r14+TSTATE.parent_fd]
    mov qword [r14+TSTATE.parent_fd], -1
    test rdi, rdi
    js .by_path
    mov rsi, [r14+TSTATE.child_name] ; the caller left its fd and our name
    jmp .do_open
.by_path:
    mov rdi, AT_FDCWD
.do_open:
    mov rax, 257                     ; openat
    mov rdx, 0x10000                 ; O_DIRECTORY
    xor r10d, r10d
    syscall
    test rax, rax
    js .fin                          ; unreadable directory: fd skips it
    mov [rsp+40], rax
    mov rax, 217                     ; getdents64
    mov rdi, [rsp+40]
    lea rsi, [r14+ent_buf]
    mov rdx, ENT_BUF
    syscall
    test rax, rax
    jle .collected
    lea rsi, [r14+ent_buf]
    lea r10, [rsi+rax]               ; end of this getdents batch
    LAST_BATCH
.parse:
    cmp rsi, r10
    jae .batch_end
    mov r11, rsi
    movzx eax, word [rsi+16]         ; d_reclen
    mov r9, rax
    movzx r8d, byte [rsi+18]         ; d_type
%ifdef FORCE_UNKNOWN_TYPE
    xor r8d, r8d                     ; test hook: every type unknown, so lstat
%endif
    lea r15, [rsi+19]                ; d_name
    cmp byte [r15], '.'
    jne .visible
    ; "." and ".." are path components, never entries: skipped whatever the
    ; flag is (every other name starting with . continues below, where the
    ; gate at the bottom of this branch decides walk-or-skip).
    cmp byte [r15+1], 0
    je .pskip                        ; "."
    cmp byte [r15+1], '.'
    jne .hidname                     ; any other dot-entry, incl. .gitignore
    cmp byte [r15+2], 0
    je .pskip                        ; ".."
.hidname:
    ; A dot-entry may walk like any other (the gate below decides), but one
    ; of them matters in every mode: .gitignore. Note that the directory
    ; lists one, so load_ignore opens a file only then; without this, every
    ; directory of a repo costs a failing openat. Compared without regard to
    ; case (OR 0x20 folds letters; the '.' and the NUL are checked exactly),
    ; because open() on a case-folding filesystem would have found it under
    ; any spelling. The 8-byte load stays inside the getdents buffer. The
    ; slot is set to git_repo, so unless ignore rules are active the
    ; recorded slot stays 0 and load_ignore is never called — no branch, no
    ; code.
    ; .git itself is pruned at the gate below, name-based and gated on
    ; git_repo; the slot logic above is untouched by it.
    mov rax, [r15]
    mov rdx, 0x2020202020202020
    or rax, rdx
    mov rdx, '.gitigno'
    cmp rax, rdx
    jne .gate
    movzx eax, word [r15+8]
    or eax, 0x2020
    cmp eax, 're'
    jne .gate
    cmp byte [r15+10], 0
    jne .gate
    mov rax, [git_repo]              ; record git_repo (1 only if --gitignore AND repo):
    mov [rsp+56], rax                ;   0 suppresses load_ignore with no branch added
.gate:
    ; .git itself, in a repo cwd under --gitignore: pruned by name. Git
    ; never tracks anything under it: its walk drops the exact .git name
    ; silently, its path checks reject the lookalike spellings outright
    ; (git add .Git -> "error: invalid path"), no ignore rule reaches it
    ; and no ! rule brings it back, so the prune behaves like a rule that
    ; always matches, applied before the rules are even loaded. Any
    ; d_type: directories, submodule marker files, dot-symlinks. Exact
    ; ".git" only — no fold, unlike the .gitignore probe above: nothing
    ; is being opened here, and a case-spelling is distinct filesystem
    ; content that must stay searched (git's own path checks reject those
    ; spellings too — recorded narrowness: the prune models only the
    ; exact name git itself writes). The loads stay
    ; inside the dirent record for every name: a reclen is a multiple of 8
    ; and always carries room for a 4-character name plus its terminator
    ; past the 19-byte fixed prefix (24 minimum), so the 4-byte compare and
    ; the NUL probe — 5 bytes, not 8 — never leave the record, let alone
    ; the buffer; no guarded read is needed and no sibling storage can be
    ; read, unlike the unguarded 8-byte probe above.
    cmp qword [git_repo], 0
    je .gate_hid
    cmp dword [r15], '.git'          ; 4 chars, packed little-endian
    jne .gate_hid
    cmp byte [r15+4], 0
    jne .gate_hid
    jmp .pskip                       ; untracked by construction: never
                                     ;   collected, never descended into
.gate_hid:
    cmp byte [hid_mode], 2
    je .pskip                        ; --no-hidden: hidden entries stay hidden
.visible:                            ; default falls through: the dot-entry
                                     ; joins the visible path like any other
    ; Keep directories and unknown types for traversal/fallback. With a filter,
    ; discard known non-directory entries whose type bit is not selected.
    ; shortcut: known d_type may misclassify bind mounts; exact classification
    ; would require statting all entries.
    cmp r8b, 4                       ; DT_DIR
    je .keep
    test r8b, r8b                    ; DT_UNKNOWN
    jz .keep
    cmp byte [type_seen], 0
    je .type_visible
    bt qword [res_type], r8
    jnc .pskip
.type_visible:
    ; Directories and unknown types were kept above; only known non-directories
    ; reach this pattern prefilter.
    mov rdx, [pat_len]
    test rdx, rdx
    jz .keep                         ; no pattern: everything matches
    mov rdi, r15
    mov rsi, [pat_ptr]
    call name_match                  ; keeps r8-r11, r15; rsi is reloaded below
    test al, al
    jz .pskip
    or r8d, 0x100                    ; separate cached-match metadata byte
.keep:
    mov rdi, [r14+TSTATE.tab_n]      ; room first: grow_table keeps r8-r11, r15
    cmp rdi, [r14+TSTATE.tab_cap]
    jb .room
    call grow_table
.room:
    mov rdi, r8
    call arena_put                   ; rax = record, rdx = key
    mov rdi, [r14+TSTATE.tab_n]
    shl rdi, 4
    add rdi, [r14+TSTATE.tab_ptr]
    mov [rdi], rdx                   ; the entry: key, then the name pointer;
    lea rax, [rax+2]                 ; two metadata bytes precede the name
    mov [rdi+8], rax
    inc qword [r14+TSTATE.tab_n]
.pskip:
    mov rsi, r11
    add rsi, r9
    jmp .parse
.batch_end:
    test r13d, r13d
    jnz .collected                   ; the last batch was short: no EOF call
.collect:
    mov rax, 217                     ; getdents64
    mov rdi, [rsp+40]
    lea rsi, [r14+ent_buf]
    mov rdx, ENT_BUF
    syscall
    test rax, rax
    jle .collected
    lea r10, [rsi+rax]               ; this batch's end, not the first one's
    LAST_BATCH
    jmp .parse
.collected:
    cmp r12, [fd_keep]               ; r12 = this directory's depth in the walk
    jb .keep_open                    ; subdirectories will open relative to it
    mov rax, 3                       ; close
    mov rdi, [rsp+40]
    syscall
    mov qword [rsp+40], -1
.keep_open:
    mov rax, [r14+TSTATE.tab_n]
    mov [rsp+16], rax                ; entries collected (counted while parsing)
    mov rax, [r14+TSTATE.tab_ptr]
    mov [rsp+24], rax
    cmp qword [rsp+56], 0
    je .no_ignore                    ; no .gitignore listed: nothing to open
    call load_ignore                 ; only path_buf[path_len..) is touched
.no_ignore:

    ; split decision: a narrow first level may split recursively; when the
    ; first level is very wide, child groups are tasks and walking inline keeps
    ; the parent-fd relative opens instead of creating one group per directory.
    cmp qword [nthreads], 1
    jbe .sorted
    cmp qword [r14+TSTATE.no_split], 0
    jne .sorted
    cmp qword [group_count], SPLIT_CAP
    jae .sorted
    mov rdx, [rsp+16]
    test rdx, rdx
    jz .sorted
    mov rsi, [rsp+24]
    xor r9d, r9d
.cnt:
    mov rax, [rsi+8]
    cmp byte [rax-2], 4
    jne .cnt_next
    inc r9
.cnt_next:
    add rsi, 16
    dec rdx
    jnz .cnt
    cmp r9, [split_min]
    jb .sorted
    mov rax, [group_count]
    add rax, r9
    cmp rax, SPLIT_CAP
    ja .sorted                       ; too wide: walk it inline instead
    mov [root_width], r9
    mov qword [rsp+32], 1
.sorted:
    mov rax, [rsp+24]
    mov rcx, [rsp+16]
    mov r15, rax
    call sort_table
    xor r15d, r15d
    cmp qword [nthreads], 1
    jbe .no_range
    mov rax, [rsp+16]
    cmp rax, RANGE_MIN
    jb .no_range
    mov rdi, [rsp+24]
    mov rsi, rax
    call try_range_split
    test eax, eax
    jnz .fin
.no_range:
    cmp qword [rsp+32], 0
    je .iter_check
    cmp qword [nthreads], 1
    jbe .iter_check
    mov rdi, [rsp+24]
    mov rsi, [rsp+16]
    call walk_wide
    jmp .fin
.iter_check:
    cmp r15, [rsp+16]
    jae .fin
    mov rdx, r15
    shl rdx, 4
    add rdx, [rsp+24]
    mov r12, [rdx+8]                 ; name
    movzx r13d, byte [r12-2]         ; raw d_type
    mov [rsp+56], r13                ; resolved type; calls below eat registers
    cmp r13b, 4                      ; DT_DIR
    je .isdir
    test r13b, r13b
    jnz .notdir
    call .mkpath                     ; DT_UNKNOWN: lstat, do not follow links
    mov rax, 262
    mov rdi, AT_FDCWD
    lea rsi, [r14+path_buf]
    lea rdx, [r14+statbuf]
    mov r10, 0x100                   ; AT_SYMLINK_NOFOLLOW
    syscall
    test rax, rax
    js .notdir
    mov eax, [r14+statbuf+24]        ; st_mode
    and eax, 0xF000
    cmp eax, 0x4000                  ; S_IFDIR
    je .unknown_dir
    cmp eax, 0x8000                  ; S_IFREG
    je .unknown_file
    jmp .notdir                      ; symlinks and special entries remain type 0
.unknown_dir:
    mov byte [rsp+56], 4             ; DT_DIR
    jmp .isdir
.unknown_file:
    mov byte [rsp+56], 8             ; DT_REG
    jmp .notdir
.isdir:
    mov r13d, 1
    call .mkpath
    jmp .resolved
.notdir:
    xor r13d, r13d
.resolved:
    mov rax, [r14+ig_chain]
    test rax, rax
    jz .check_match
.ig_check:
    call .mkpath                     ; ig_ignored() wants the path
    mov rdi, r12
    lea rsi, [r14+path_buf]
    mov rdx, r13
    call ig_ignored
    test al, al
    jnz .next
.check_match:
    test byte [r12-1], 1             ; parse-time cached name match
    jnz .name_ok
    mov rdx, [pat_len]
    test rdx, rdx
    jz .name_ok                      ; no pattern still needs the type gate
    mov rdi, r12
    mov rsi, [pat_ptr]
    call name_match                  ; directories and unknown types only
    test al, al
    jz .descend
.name_ok:
    cmp byte [type_seen], 0
    je .matched
    movzx ecx, byte [rsp+56]
    bt qword [res_type], rcx
    jnc .descend
.matched:
    call .mkpath                     ; print_entry() wants the path
    mov rdi, rbp
    mov rsi, r13
    call print_entry
.descend:
    test r13, r13
    jz .next
    mov [r14+path_len], rbp
    mov rax, [rsp+40]
    mov [r14+TSTATE.parent_fd], rax  ; -1 if this directory is closed already
    mov [r14+TSTATE.child_name], r12
    call walk_dir
    mov [r14+path_len], rbx
.next:
    inc r15
    jmp .iter_check

; mkpath: copy the entry name (r12) after the directory path in path_buf;
; rbp = the entry's path length. Plain files that neither match nor carry
; ignore rules skip this: most entries are those.
.mkpath:
    lea rdi, [r14+path_buf]
    add rdi, rbx
    test rbx, rbx
    jz .nols
    mov byte [rdi], '/'
    inc rdi
.nols:
    mov rsi, r12
.cp:
    mov al, [rsi]
    mov [rdi], al
    inc rsi
    inc rdi
    test al, al
    jnz .cp
    lea rax, [r14+path_buf]
    sub rdi, rax
    dec rdi
    mov rbp, rdi                     ; entry path length
    ret
.fin:
    mov rdi, [rsp+40]
    test rdi, rdi
    js .closed
    mov rax, 3                       ; close
    syscall
.closed:
    dec qword [r14+f_deep]
    mov rax, [rsp+48]
    mov [r14+TSTATE.sc_top], rax     ; names, table and rules of this directory
    mov rax, [rsp]
    mov [r14+ig_nrules], rax
    mov rax, [rsp+8]
    mov [r14+ig_chain], rax
    add rsp, 64
    pop r15
    pop r14
    pop r13
    pop r12
    pop rbp
    pop rbx
    ret

; walk_range: execute a range group.  The group owns a sorted copy of its
; slice's table and names.  Directories are walked inline, in range order, so
; the range output stays contiguous and cannot depend on another worker.
walk_range:
    push rbx
    push rbp
    push r12
    push r13
    push r15
    sub rsp, 32
    mov rbx, [r14+path_len]          ; base path length
    xor r15d, r15d
.loop:
    mov rax, [r14+cur_grp]
    mov rcx, [rax+GRP_N]
    cmp r15, rcx
    jae .done
    mov rdx, [rax+GRP_TAB]
    mov rax, r15
    shl rax, 4
    add rax, rdx
    mov r12, [rax+8]                 ; name record
    movzx r13d, byte [r12-2]         ; raw d_type
    mov [rsp+0], r13                 ; resolved type
    cmp r13b, 4
    je .isdir
    test r13b, r13b
    jnz .notdir
    call walk_dir.mkpath
    mov rax, 262
    mov rdi, AT_FDCWD
    lea rsi, [r14+path_buf]
    lea rdx, [r14+statbuf]
    mov r10, 0x100
    syscall
    test rax, rax
    js .unknown_other
    mov eax, [r14+statbuf+24]
    and eax, 0xF000
    cmp eax, 0x4000
    je .unknown_dir
    cmp eax, 0x8000
    je .unknown_file
.unknown_other:
    xor r13d, r13d
    jmp .resolved
.unknown_dir:
    mov byte [rsp+0], 4
    mov r13d, 1
    jmp .resolved
.unknown_file:
    mov byte [rsp+0], 8
    xor r13d, r13d
    jmp .resolved
.isdir:
    call walk_dir.mkpath
    mov r13d, 1
    mov byte [rsp+0], 4
    jmp .resolved
.notdir:
    call walk_dir.mkpath
    xor r13d, r13d
.resolved:
    mov rax, [r14+ig_chain]
    test rax, rax
    jz .check_match
    mov rdi, r12
    lea rsi, [r14+path_buf]
    mov rdx, r13
    call ig_ignored
    test al, al
    jnz .next
.check_match:
    test byte [r12-1], 1
    jnz .name_ok
    mov rdx, [pat_len]
    test rdx, rdx
    jz .name_ok
    mov rdi, r12
    mov rsi, [pat_ptr]
    call name_match
    test al, al
    jz .descend
.name_ok:
    cmp byte [type_seen], 0
    je .matched
    movzx ecx, byte [rsp+0]
    bt qword [res_type], rcx
    jnc .descend
.matched:
    mov rdi, rbp
    mov rsi, r13
    call print_entry
.descend:
    test r13, r13
    jz .next
    mov [r14+path_len], rbp
    mov qword [r14+TSTATE.parent_fd], -1
    mov [r14+TSTATE.child_name], r12
    call walk_dir
    mov [r14+path_len], rbx
.next:
    inc r15
    jmp .loop
.done:
    add rsp, 32
    pop r15
    pop r13
    pop r12
    pop rbp
    pop rbx
    ret
