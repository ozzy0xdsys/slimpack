"""numpy test: the hard case - a C-extension package whose native BLAS/LAPACK
DLL lives in a sibling numpy.libs/ directory, loaded via os.add_dll_directory.
np.linalg and matmul force the OpenBLAS DLL to load."""

import numpy as np


def main():
    a = np.arange(12, dtype=float).reshape(3, 4)
    print("version    :", np.__version__)
    print("sum        :", a.sum())
    print("mean       :", a.mean())
    print("norm       :", round(float(np.linalg.norm(a)), 6))   # LAPACK/BLAS
    b = a @ a.T                                                 # OpenBLAS matmul
    print("matmul[0,0]:", b[0, 0])
    inv = np.linalg.inv(np.eye(3) * 2.0)                        # LAPACK
    print("inv diag   :", inv.diagonal().tolist())
    print("OK")


if __name__ == "__main__":
    main()
