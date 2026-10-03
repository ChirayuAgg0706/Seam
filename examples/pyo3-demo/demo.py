import seam_demo


def report(n):
    label = "squares"
    result = seam_demo.sum_squares(n)  # step in here
    return "%s(%d) = %d" % (label, n, result)


print(report(5))
