zfind: zfind.asm
	nasm -f elf64 -g $< -o zfind.o
	ld -o $@ zfind.o

run: zfind
	./zfind

clean:
	rm -f zfind zfind.o

.PHONY: run clean
