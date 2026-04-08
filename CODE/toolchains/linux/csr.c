// csr_test.c
int main() {
    unsigned long x;

    // Read mstatus CSR
    asm volatile ("csrrc %0, mstatus" : "=r"(x));

    // Set a bit in mstatus
    asm volatile ("csrs mstatus, %0" :: "r"(1));

    return 0;
}
