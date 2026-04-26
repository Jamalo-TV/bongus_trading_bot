import joblib
import numpy as np
import os
from core.state import METRICS

class FundingPredictor:
    def __init__(self, model_path: str = "models/funding_model.joblib"):
        self.model_path = model_path
        if os.path.exists(self.model_path):
            try:
                self.model = joblib.load(self.model_path)
                METRICS["model_loaded"] = True
            except Exception:
                self.model = None
                METRICS["model_loaded"] = False
        else:
            self.model = None
            METRICS["model_loaded"] = False

    def predict(self, features: np.ndarray) -> float:
        if self.model is None:
            return 0.0
        
        prediction = self.model.predict(features)
        if isinstance(prediction, np.ndarray):
            return prediction[0]
        return prediction
