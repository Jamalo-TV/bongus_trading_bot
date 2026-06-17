import pytest
import os
import time
import numpy as np
import joblib
from ml_pipeline.trainer import FundingModelTrainer
from market_data.funding_predictor import FundingPredictor

def test_trainer_saves_model():
    model_path = "tests/test_model.joblib"
    if os.path.exists(model_path):
        os.remove(model_path)
    
    trainer = FundingModelTrainer(model_path=model_path)
    # Generate mock training data
    X = np.random.rand(100, 5)
    y = np.random.rand(100)
    
    trainer.train(X, y)
    assert os.path.exists(model_path)
    
    # Cleanup
    os.remove(model_path)

def test_predictor_performance():
    model_path = "tests/perf_model.joblib"
    trainer = FundingModelTrainer(model_path=model_path)
    X = np.random.rand(100, 5)
    y = np.random.rand(100)
    trainer.train(X, y)
    
    predictor = FundingPredictor(model_path=model_path)
    
    # Mock input
    test_input = np.random.rand(1, 5)
    
    start_time = time.time()
    prediction = predictor.predict(test_input)
    end_time = time.time()
    
    duration_ms = (end_time - start_time) * 1000
    assert duration_ms < 5.0
    assert type(prediction).__name__ in ('float', 'float64', 'ndarray')
    
    # Cleanup
    os.remove(model_path)
