/* A header with code of its own: breakpoints in included files go through the same path
 * mapping as the file that includes them. */
__attribute__((noinline)) static long
halve(long value)
{
    long half = value / 2;
    return half; /* halve-return */
}
