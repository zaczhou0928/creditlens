import argparse
from app.demo_data import seed_demo_data

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成零售信贷合成数据")
    parser.add_argument("--applications", type=int, default=1200)
    parser.add_argument("--seed", type=int, default=20261006)
    args = parser.parse_args()
    print(seed_demo_data(seed=args.seed, applications=args.applications))
