import joblib
import numpy as np
import os

class FundingPredictor:
    def __init__(self, model_path: str = "models/funding_model.joblib"):
        self.model_path = model_path
        if os.path.exists(self.model_path):
            self.model = joblib.load(self.model_path)
        else:
            self.model = None

    def predict(self, features: np.ndarray) -> float:
        if self.model is None:
            # Fallback if model not loaded
            return 0.0
        
        prediction = self.model.predict(features)
        if isinstance(prediction, np.ndarray):
            return prediction[0]
        return prediction
