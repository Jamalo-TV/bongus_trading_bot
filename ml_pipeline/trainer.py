import joblib
from sklearn.linear_model import Ridge
import numpy as np

class FundingModelTrainer:
    def __init__(self, model_path: str = "models/funding_model.joblib"):
        self.model_path = model_path
        self.model = Ridge()

    def train(self, X: np.ndarray, y: np.ndarray):
        self.model.fit(X, y)
        joblib.dump(self.model, self.model_path)
