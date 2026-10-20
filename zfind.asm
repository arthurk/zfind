default rel                     ; x86-64: address labels RIP-relative, not via implicit ABS

; zfind, "simple search" use case only:
;   zfind [PATTERN]     search cwd for entries whose name matches PATTERN
; PATTERN is a literal substring (case-insensitive unless it contains A-Z,
; "smart case"); regex and glob syntax are not implemented.
; Hidden entries are skipped and .gitignore rules are applied when cwd is a
; git repository, but the pattern is matched against the name only.
; shortcut: no options or path arguments, no regex/glob, and none of the
;   ignore sources beyond .gitignore in the walked tree itself (no parent
;   dirs, no global gitignore, no .ignore, no [] or \ in patterns);
;   add use-case by use-case.
;
; The walk is parallel. Thread 0 walks down until a directory holds
; at least 2*threads subdirectories, then hands each of those subtrees to the
; other threads as a task and keeps walking. The main thread does no walking:
; it only writes. Every unit of output - a task's subtree, or a run of lines the
; dispatching walk printed itself - is a "record": a chain of 16 KiB blocks that
; its producer appends lines to while the writer is already reading them. The
; dispatching walk publishes one token (a record pointer) per record, in DFS
; order; workers claim the tokens in that order and the writer replays them in
; that order, so the single-threaded byte order is reproduced exactly, and the
; first lines reach stdout while the walk is still running.
;
; Memory is bounded two ways. Names, sort tables and ignore rules live in a
; per-thread scratch region with stack discipline: walk_dir puts the scratch top
; back when it returns, so a directory's entries die with its frame. Output
; blocks go back to the thread that made them as soon as the writer has written
; them, and a producer more than BLK_LIMIT blocks ahead of the writer waits -
; except the one the writer is waiting on, so the wait cannot deadlock. Only
; records, tokens, rule chain nodes of the dispatching walk and the blocks
; themselves come from the shared arena.
; shortcut: subtrees are split at the first level with >= 2*threads
;   subdirectories, so a tree whose every level fans out less than that is
;   still walked single-threaded; descend deeper when that shows up in a bench.
; shortcut: the writer streams the record it is on as it grows (polling every
;   5 ms for new lines) but an unfinished record further ahead is not written
;   until it is the current one, which is what keeps the order exact.

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
%define IGBUF_SZ    16384            ; .gitignore bytes read per directory
%define IG_DEPTH    32               ; deepest directory whose .gitignore is read
%define IG_MAX      1024             ; rules kept at once (per thread)
%define ARENA_SZ    (1 << 30)        ; records, tokens, output blocks: one mmap
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
%define BLK_LIMIT   1024             ; blocks in flight before producers wait
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
  .igbuf:     resb IGBUF_SZ * IG_DEPTH
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
  .tab_ptr:   resq 1                ; {key, name} table (16 bytes each) of the current dir
  .tab_n:     resq 1
  .tab_cap:   resq 1
  .blk_free:  resq 1                ; blocks the writer gave back to us
  .mode:      resq 1                ; 1 = dispatching walk (main thread only)
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
%define mode       TSTATE.mode

%define TSTATE_STRIDE ((TSTATE_size + 63) & ~63)

; record (arena): one unit of output. A task has a path and rule chain to walk;
; a text record holds lines the dispatching walk printed itself.
%define REC_PATH    0
%define REC_PATHLEN 8
%define REC_CHAIN   16
%define REC_HEAD    24               ; first output block (0 until there is output)
%define REC_KIND    32               ; 0 = task, 1 = text
%define REC_DONE    40               ; dword, set after the last block is final
%define REC_SZ      64
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
    e_write     db "zfind: write error", 10
    e_write_e:
    e_big       db "zfind: tree too large", 10
    e_big_e:
    e_scratch   db "zfind: directory too large", 10
    e_scratch_e:
    h_short     db "-h", 0
    h_long      db "--help", 0
    z_short     db "-0", 0
    z_long      db "--null", 0
    help_msg    db "usage: zfind [-h] [-0] [PATTERN]", 10, 10
               db "Search the current directory tree for entries whose name contains", 10
               db "PATTERN, a literal substring (no regex or glob). Case-insensitive", 10
               db "unless PATTERN contains an A-Z. Without a pattern every entry is", 10
               db "listed.", 10, 10
               db "Hidden entries are skipped and .gitignore is applied when the", 10
               db "current directory is a git repository. Output is sorted by name.", 10, 10
               db "  -h, --help  show this help and exit", 10
               db "  -0, --null  separate entries with NUL, not newline", 10
    help_msg_e:
    align 8
    poll_ts     dq 0, 5000000         ; 5 ms: how stale streamed output can get

section .bss
    arena_base: resq 1
    g_resv:     resq 1                ; next free arena byte (xadd'd)
    pat_ptr:    resq 1
    pat_len:    resq 1
    pat_ci:     resq 1                ; 1 = fold case (pattern has no A-Z)
    nul_sep:    resq 1                ; 1 = separate entries with NUL, not newline
    git_repo:   resq 1
    tstate_map: resq 1
    stacks:     resq 1
    scratch_map: resq 1
    scratch_sz: resq 1                ; per thread
    nthreads:   resq 1                ; walking threads; the main thread writes
    split_min:  resq 1                ; dispatch a level with this many subdirs
    fd_keep:    resq 1                ; per-thread budget of directories kept open
    rl_buf:     resq 2
    tok_ptr:    resq 1                ; token stream: one record pointer each
    tok_n:      resq 1                ; published count (dword); thread 0 writes
    tok_cap:    resq 1
    tok_claim:  resq 1                ; next token a worker may claim
    walk_done:  resq 1                ; dword: the token stream is complete
    ev_seq:     resq 1                ; dword futex word: tokens / done / walk_done
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
    cmp rbx, 1
    je .no_pat
    mov r13, [rbp+8]                 ; argv[1]
    cmp byte [r13], '-'
    jne .plain
    mov rdi, r13
    lea rsi, [z_short]                ; "-0"
    call streq
    je .null
    mov rdi, r13
    lea rsi, [z_long]                 ; "--null"
    call streq
    je .null
    mov rdi, r13
    lea rsi, [h_short]                ; "-h": help, before the '--' checks below
    call streq
    je .help
    cmp byte [r13+1], '-'
    jne .plain
    mov rdi, r13
    lea rsi, [h_long]                 ; "--help"
    call streq
    je .help
    cmp byte [r13+2], 0
    jne .plain
    cmp rbx, 2                       ; "--" alone: no pattern
    je .no_pat
    cmp rbx, 3                       ; "-- PATTERN"
    jne bad_args
    mov r13, [rbp+16]
    jmp .pat
.plain:
    cmp rbx, 2                       ; anything else would be a search path
    jne bad_args
    jmp .pat                         ; the pattern, not a flag: no -0 here
.null:                                ; -0/--null: NUL-separated output. Like
    mov qword [nul_sep], 1            ; "--", it takes an optional pattern and
    cmp rbx, 3                       ; nothing else
    je .pat2
    cmp rbx, 2
    jne bad_args
    jmp .no_pat
.pat2:
    mov r13, [rbp+16]
.pat:
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
.pat_ready:
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

    ; .gitignore applies only inside a git repository; .git in cwd is
    ; the shortcut here (no ancestor walk, see the header)
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
    mov rax, [nthreads]
    shl rax, 1                       ; dispatch a level with 2*threads subdirs
    mov [split_min], rax

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

; cpu_count -> rax = walking thread count, 2..MAX_THREADS (the main thread
; writes and is not counted). -DFORCE_THREADS=n pins it for stress tests.
cpu_count:
%ifdef FORCE_THREADS
    mov eax, FORCE_THREADS
    ret
%endif
    mov eax, 204                     ; sched_getaffinity
    xor edi, edi                     ; pid 0 = this thread
    lea rsi, [cpuset]
    mov edx, 128
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
; and its own stack. Thread 0 first runs the dispatching walk, which publishes
; tokens as it goes so the other threads start on subtrees at once; then it
; joins them.
thread_main:
    mov r14, r13
    mov [r14+TSTATE.id], r12
    mov qword [r14+TSTATE.parent_fd], -1
    test r12, r12
    jnz .work
    mov qword [r14+TSTATE.mode], 1
    call walk_dir                    ; depth 0 .. the first wide level
    call finish_dispatch
.work:
    call run_tasks
    mov eax, 60                      ; SYS_exit: leave the other threads alone
    xor edi, edi
    syscall

; finish_dispatch: close the open text record and publish "no more tokens"
finish_dispatch:
    mov rdi, [r14+cur_task]
    test rdi, rdi
    jz .end
    mov qword [r14+cur_task], 0
    mov qword [r14+cur_blk], 0
    call mark_done
.end:
    mov dword [walk_done], 1
    jmp notify_ev

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

; run_tasks: claim tokens in order and walk the tasks among them, until the
; stream is complete and every token is claimed
run_tasks:
    push rbx
    push r12
    push r13
.claim:
    mov r12d, [ev_seq]               ; before the checks, see ev_sleep
    mov r13d, [walk_done]            ; before tok_n: a 1 means tok_n is final
    mov rax, [tok_claim]
    mov ecx, [tok_n]
    cmp rax, rcx
    jae .none
    lea rdx, [rax+1]
    lock cmpxchg [tok_claim], rdx    ; rax is the implicit expected value
    jne .claim
    mov rcx, [tok_ptr]               ; after tok_n: the array has that many entries
    mov rbx, [rcx+rax*8]
    cmp qword [rbx+REC_KIND], 0
    jne .claim                       ; text record: thread 0 fills and closes it
    mov rdi, rbx
    call run_task
    mov rdi, rbx
    call mark_done
    jmp .claim
.none:
    test r13d, r13d
    jnz .exit
    mov edx, r12d
    xor r10d, r10d                   ; no timeout: a token or walk_done will come
    call ev_sleep
    jmp .claim
.exit:
    pop r13
    pop r12
    pop rbx
    ret

; run_task: rdi = task record. Walk its subtree exactly as the dispatching walk
; would have, appending every printed line to the task's block chain.
run_task:
    push rbx
    push rbp
    push r12
    push r13
    push r15
    mov rbx, rdi
    mov rbp, [rdi+REC_PATH]
    mov rcx, [rdi+REC_PATHLEN]
    lea rdi, [r14+path_buf]
    mov [r14+path_len], rcx
    inc rcx                          ; path + NUL
    mov rsi, rbp
    rep movsb
    mov rax, [rbx+REC_CHAIN]
    mov [r14+ig_chain], rax
    mov qword [r14+ig_nrules], 0
    mov qword [r14+f_deep], 0
    mov qword [r14+TSTATE.mode], 0   ; lines go to the task, not to the token stream
    mov [r14+cur_task], rbx
    mov qword [r14+cur_blk], 0
    call walk_dir
    mov qword [r14+cur_task], 0
    pop r15
    pop r13
    pop r12
    pop rbp
    pop rbx
    ret

; mark_done: rdi = record. Its blocks and HEAD are final; tell the writer.
mark_done:
    mov dword [rdi+REC_DONE], 1
    jmp notify_ev

; emit_main: the main thread. Replays the token stream in order, writing each
; record as its producer fills it, and flushes whenever it has to wait, so
; output is as fresh as the walk can make it.
emit_main:
    push rbx
    push rbp
    push r12
    push r13
    push r14
    push r15
    xor r12d, r12d                   ; next token to write
.tok:
    mov r15d, [ev_seq]               ; before the checks, see ev_sleep
    mov r13d, [walk_done]            ; before tok_n: a 1 means tok_n is final
    mov eax, [tok_n]
    cmp r12d, eax
    jb .have
    test r13d, r13d
    jnz .finish
    call out_flush                   ; about to wait: do not sit on output
    mov edx, r15d
    xor r10d, r10d
    call ev_sleep
    jmp .tok
.have:
    mov rax, [tok_ptr]
    mov rbx, [rax+r12*8]
    mov [emit_cur], rbx              ; this record's producer is never throttled
    lock inc dword [bp_seq]
    cmp dword [bp_waiters], 0
    je .go
    lea rdi, [bp_seq]
    mov esi, FUTEX_WAKE_PRIV
    mov edx, WAKE_ALL
    mov eax, 202
    syscall
.go:
    call emit_record
    inc r12
    jmp .tok
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
out_put:
    push rbx
    push r12
    mov rbx, rdi
    mov r12, rsi
.loop:
    test r12, r12
    jz .done
    mov rcx, [out_len]
    mov rax, OUT_SZ
    sub rax, rcx                     ; room left
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
    mov rcx, rdx                     ; bytes for this run
    rep movsb
    add [out_len], rdx
    sub r12, rdx
    add rbx, rdx
    cmp qword [out_len], OUT_SZ
    jne .loop
    call out_flush
    jmp .loop
.done:
    pop r12
    pop rbx
    ret

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
    ; round the request up: tokens tag a pointer with bit 0, so every block has
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

; ig_alloc: rdi = size. Rules and chain nodes of the dispatching walk are
; referenced by tasks that outlive its frames: arena. A worker's die with the
; directory that loaded them: scratch.
ig_alloc:
    cmp qword [r14+TSTATE.mode], 0
    jne alloc
    jmp salloc

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
; only folds the name byte: pat_ci=1 means the pattern has no A-Z (see
; _start), so folding a pattern byte is the identity.
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

; The table of a directory holds 16-byte entries {key, name pointer}. The key is
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
; The dispatching walk opens a text record - and a token for it - on its first
; line after a task. With -0 the separator is a NUL instead of the newline.
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
    xor eax, eax                     ; -0: NUL, so paths with newlines survive
.sep:
    mov byte [rdi], al
    add [rdx+BLK_USED], r12          ; publish
    pop r13
    pop r12
    pop rbx
    ret

; new_text_record: the dispatching walk starts a record for its own lines
new_text_record:
    mov rdi, REC_SZ
    call alloc
    mov qword [rax+REC_HEAD], 0
    mov qword [rax+REC_KIND], 1
    mov dword [rax+REC_DONE], 0
    mov [r14+cur_task], rax
    mov qword [r14+cur_blk], 0
    mov rdi, rax
    jmp push_token

; push_token: rdi = record. Appends it to the stream, then publishes: the array
; is complete up to tok_n for any reader that loads tok_n first and tok_ptr after.
push_token:
    push rbx
    mov rbx, rdi
    mov eax, [tok_n]
    cmp rax, [tok_cap]
    jb .have
    mov rcx, [tok_cap]               ; grow: double into fresh arena bytes
    test rcx, rcx
    jz .first
    shl rcx, 1
    jmp .store
.first:
    mov rcx, 256                     ; first token: no array yet
.store:
    mov [tok_cap], rcx
    mov rdi, rcx
    shl rdi, 3
    call alloc
    mov ecx, [tok_n]
    shl rcx, 3
    mov rdi, rax
    mov rsi, [tok_ptr]
    rep movsb
    mov [tok_ptr], rax               ; readers of the old array stay valid: never freed
    mov eax, [tok_n]
.have:
    mov rsi, [tok_ptr]
    mov [rsi+rax*8], rbx
    inc dword [tok_n]                ; the store that publishes
    call notify_ev
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
    ; directory at this depth; rules handed to another thread (as a chain
    ; node) outlive it, hence ig_alloc
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
; igbuf[depth] and append its rules; rbx = directory path length, r12 = depth.
; If it added any, publish a chain node holding a copy of the whole active
; rule stack, so tasks dispatched below it keep their ancestors' rules.
load_ignore:
    push rbx
    push r12
    push r13
    sub rsp, 32
    cmp qword [git_repo], 0
    je .ret
    cmp r12, IG_DEPTH
    jae .ret                         ; deeper than the slot table
    mov [rsp], rbx                   ; ig_add_line wants the prefix length
    mov [rsp+8], r12
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
    mov r9, [rsp+8]
    shl r9, 14                       ; 16384 = 1 << 14
    lea rsi, [r14+igbuf]
    add rsi, r9                      ; this depth's slot
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
    add r8, r9                       ; slot base
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

; arena_put: append one record [d_type][name,0] to the thread's scratch.
; rdi = d_type, r15 = the dirent's d_name -> rax = the record, rdx = the sort key
; (first 8 name bytes, big-endian, zero after the NUL). The length pass could be
; avoided by allocating d_reclen, but that wastes ~27 bytes per entry (~80 MB on
; /nix/store). Clobbers rax, rcx, rdx, rsi, rdi - not r8-r11/r14/r15, which the
; directory loop is holding.
arena_put:
    push rbx
    push r12
    push r13
    mov rbx, r15
    mov r12, rdi                    ; d_type
    xor r13d, r13d
.len:
    cmp byte [rbx+r13], 0
    je .lend
    inc r13
    jmp .len
.lend:
    lea rdi, [r13+2]
    call salloc
    mov [rax], r12b
    lea rdi, [rax+1]
    mov rsi, rbx
    lea rcx, [r13+1]
    rep movsb
    mov rdx, [rax+1]                ; the key, from the copy (a guard follows it)
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

; walk_dir: search the directory at path_buf[0..path_len) (empty = cwd).
; Entries are collected first, sorted by name, then printed and recursed in
; that order. Collecting already applies the pattern: only directories, entries
; of unknown type and names that match are kept, so with a selective pattern the
; sort and the scratch hold a handful of entries instead of the whole
; directory. Sorting is never skipped, whatever the directory size.
; In mode=1 (thread 0's dispatching walk) a directory with enough
; subdirectories hands each of them to a worker instead of descending, and the
; lines printed here go to text records that the token stream interleaves with
; the tasks. Everything the frame allocates in scratch (names, table, and in a
; worker its rules) is released when it returns: [rsp+48] holds the mark.
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
    ; [rsp+56]=while parsing: this directory lists a .gitignore; while iterating:
    ;          the current entry was matched during parsing
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
    ; Hidden entries are skipped, but one of them matters: .gitignore. Note that
    ; the directory lists one, so load_ignore opens a file only then; without
    ; this, every directory of a repo costs a failing openat. Compared without
    ; regard to case (OR 0x20 folds letters; the '.' and the NUL are checked
    ; exactly), because open() on a case-folding filesystem would have found
    ; it under any spelling. The 8-byte load stays inside the getdents buffer.
    mov rax, [r15]
    mov rdx, 0x2020202020202020
    or rax, rdx
    mov rdx, '.gitigno'
    cmp rax, rdx
    jne .pskip
    movzx eax, word [r15+8]
    or eax, 0x2020
    cmp eax, 're'
    jne .pskip
    cmp byte [r15+10], 0
    jne .pskip
    mov qword [rsp+56], 1
    jmp .pskip                       ; ".", "..", and every hidden entry
.visible:
    ; Filter before collecting. A name that does not match the pattern only
    ; matters if the entry is a directory (walked whatever its name) or of
    ; unknown type (it might be one). Anything else would be neither printed
    ; nor descended into, so it gets no scratch, no table slot, no sort time.
    mov rdx, [pat_len]
    test rdx, rdx
    jz .keep                         ; no pattern: everything matches
    cmp r8b, 4                       ; DT_DIR
    je .keep
    test r8b, r8b                    ; DT_UNKNOWN
    jz .keep
    mov rdi, r15
    mov rsi, [pat_ptr]
    call name_match                  ; keeps r8-r11, r15; rsi is reloaded below
    test al, al
    jz .pskip
    or r8d, 0x80                     ; bit 7 of the stored type: matched already
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
    lea rax, [rax+1]                 ; the d_type byte sits right before it
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

    ; split decision: a level wide enough to keep every worker busy is worth
    ; handing out, whatever its depth
    cmp qword [r14+TSTATE.mode], 0
    je .sorted
    mov rdx, [rsp+16]
    test rdx, rdx
    jz .sorted
    mov rsi, [rsp+24]
    xor r9d, r9d
.cnt:
    mov rax, [rsi+8]
    cmp byte [rax-1], 4
    jne .cnt_next
    inc r9
.cnt_next:
    add rsi, 16
    dec rdx
    jnz .cnt
    cmp r9, [split_min]
    jb .sorted
    mov qword [rsp+32], 1
.sorted:
    mov rax, [rsp+24]
    mov rcx, [rsp+16]
    mov r15, rax
    call sort_table
    xor r15d, r15d
.iter_check:
    cmp r15, [rsp+16]
    jae .fin
    mov rdx, r15
    shl rdx, 4
    add rdx, [rsp+24]
    mov r12, [rdx+8]                 ; name
    movzx r13d, byte [r12-1]         ; d_type, with bit 7 set if the parse matched it
    mov eax, r13d
    and eax, 0x80
    mov [rsp+56], rax                ; in the frame: the calls below eat registers
    and r13d, 0x7f
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
    jne .notdir
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
    cmp qword [rsp+56], 0
    jne .matched                     ; the parse already matched this name
    mov rdx, [pat_len]
    test rdx, rdx
    jz .matched                      ; no pattern
    mov rdi, r12
    mov rsi, [pat_ptr]
    call name_match                  ; directories and unknown types only
    test al, al
    jz .descend
.matched:
    call .mkpath                     ; print_entry() wants the path
    mov rdi, rbp
    mov rsi, r13
    call print_entry
.descend:
    test r13, r13
    jz .next
    cmp qword [rsp+32], 0
    jne .dispatch
    mov [r14+path_len], rbp
    mov rax, [rsp+40]
    mov [r14+TSTATE.parent_fd], rax  ; -1 if this directory is closed already
    mov [r14+TSTATE.child_name], r12
    call walk_dir
    mov [r14+path_len], rbx
    jmp .next
.dispatch:
    call .mkpath
    mov rdi, rbp
    call dispatch_task
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

; dispatch_task: rdi = path length of a subtree root. Closes the text record the
; walk was filling, then makes a task record plus a copy of its path in the
; arena and publishes its token; the walk continues with the next entry, so the
; token order is the DFS order.
dispatch_task:
    push rbx
    push r12
    push r13
    mov rbx, rdi
    mov rdi, [r14+cur_task]
    test rdi, rdi
    jz .fresh
    mov qword [r14+cur_task], 0
    mov qword [r14+cur_blk], 0
    call mark_done
.fresh:
    mov rdi, REC_SZ
    call alloc
    mov r12, rax
    mov rax, [r14+ig_chain]
    mov [r12+REC_CHAIN], rax
    mov qword [r12+REC_HEAD], 0
    mov qword [r12+REC_KIND], 0
    mov dword [r12+REC_DONE], 0
    mov [r12+REC_PATHLEN], rbx
    lea rdi, [rbx+1]                 ; path + NUL
    call alloc
    mov r13, rax
    mov [r12+REC_PATH], rax
    mov rdi, rax
    lea rsi, [r14+path_buf]
    mov rcx, rbx
    inc rcx
    rep movsb
    mov rdi, r12
    call push_token
    pop r13
    pop r12
    pop rbx
    ret
