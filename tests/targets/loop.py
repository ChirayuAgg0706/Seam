def work(i):
    value = i * i  # work-body
    return value


def main():
    total = 0
    for i in range(10):
        total += work(i)  # loop-call
    print("total", total)


main()
