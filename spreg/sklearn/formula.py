__author__ = "Tyler D. Hoffman pysal@tdhoffman.com"

import numpy as np
import pandas as pd
import geopandas as gpd
from formulaic import model_matrix
from sklearn.linear_model import LinearRegression
from .lag import Lag
from .error import Error


def noninplace_remove(lst, el):
    """
    Helper function to remove an element from a list and return the list.
    """
    
    lst.remove(el)
    return lst


def expand_lag(w_name, fields):
    """
    Helper function for `from_formula`.
    Creates a formula string with spatial lag terms for the given fields.
    """

    fields = [fields] if type(fields) == str else fields
    output = ["{" + w_name + ".sparse @ `" + field + "`}" for field in fields]
    return " + ".join(output)


def from_formula(formula, df, w=None, method="gm", debug=False,
                 fit_kwargs={}, init_kwargs={}): 
    """
    Given a formula and a dataframe, parse the formula and return a configured
    `spreg` model.
    
    Inputs
    ------
    
    formula  : string
               formula description following formulaic's grammar and the below syntax
    df       : pandas.DataFrame or geopandas.GeoDataFrame
               container with columns labelled according to the desired variables
    w        : libpysal.weights.W
               spatial weights matrix for spatial models. Default set to None.
    method   : string
               estimation method: "gm" for generalized method of moments,
               "full" for brute force ML estimation, "lu" for ML estimation with
               LU log Jacobian calculation, or "ord" for ML estimation with Ord
               log Jacobian calculation. Default set to "gm".
    debug    : boolean
               if true, outputs the preprocessed formula alongside the spatial model.
               Default set to False.
    init_kwargs : dictionary
                  additional keyword arguments to be passed to the underlying model class.
    fit_kwargs  : dictionary
                  additional keyword arguments to be passed to the underlying model fit.


    Description
    -----------
    Syntax for the formulas is the same as `formulaic`, with two spatial operators added.

    - The `<...>` operator:
        - Enclose variables (covariates or response) in angle brackets to 
          denote a spatial lag.
        - `<` and `>` are not reserved characters in `formulaic` (the underlying 
          formula parsing library), so there are no conflicts.
        - **Usage:** include `<FIELD>` as a term in a formula string to add 
          that field and its spatial lag field from the dataframe to model matrix.
        - Can use other transformations within `<...>`, e.g. 
          `<{10*FIELD1} + FIELD2>` will be correctly parsed.
    - The `&` symbol:
        - Adds a spatial error component to a model.
        - `&` is not a reserved character in formulaic, so there are no conflicts.
        - **Usage:** include `&` as a term in a formula string to introduce 
          a spatial error component in a model.

    The parser accepts combinations of `<...>` and `&`: `<FIELD1 + ... + FIELDN> + &` 
    is the most general possible spatial model available. However, unlike in the default
    `spreg.from_formula()` function, the parser does not accept Combo models (spatial lag 
    of y and spatial error components). Durbin models are accepted (`DurbinError` is spatial
    lag of X and spatial error; `DurbinLag` is spatial lag of X and spatial lag of y), 
    but are passed directly to the `Error` and `Lag` classes instead of referring to 
    the syntactic sugar Durbin model classes. Additionally, any transformations from 
    [`formulaic`'s grammar](https://matthewwardrop.github.io/formulaic/concepts/grammar/) 
    are admissible anywhere in the formula string.

    Variable names do not need to be provided separately to the model classes 
    (i.e., through the `name_x` and `name_y` keyword arguments) as they can be 
    automatically detected from the formula string. Variables which read 
    ``w.sparse @ `FIELD` `` are the spatial lags of those fields 
    (future TODO: make this prettier).
    
    The skedastic option is not supported in this version of the function
    as hetero/homoskedastic errors are not supported by the sklearn-style models.

    Finally, if `yend` is included as a keyword argument for an error model,
    the dispatcher will send it to the correct endogenous error model.
    

    Examples
    --------

    Import necessary libraries.

    >>> import spreg.sklearn
    >>> import numpy as np
    >>> import geopandas as gpd
    >>> from libpysal.examples import load_example
    >>> from libpysal.weights import Kernel, fill_diagonal
    
    Load the Boston housing example.

    >>> boston = load_example("Bostonhsg")
    >>> boston_df = gpd.read_file(boston.get_path("boston.shp"))
    
    Create a weights matrix and set its diagonal to zero (necessary for spatial
    lag of y model).

    >>> weights = Kernel(boston_df[["x", "y"]], k=50, fixed=False)
    >>> weights = fill_diagonal(weights, 0)
    
    Write a formula. This formula incorporates transformations, a spatial lag of
    a covariate, a spatial lag of the dependent variable, and a spatial error component.

    >>> formula = "log(CMEDV) ~ {RM**2} + AGE + <log(CMEDV) + INDUS>"
    
    Fit model and output the formula as well. Notice that there's a "- 1" at the end --
    that's so that the underlying model classes will behave correctly if `fit_intercept`
    is given as an element in `fit_kwargs`.

    >>> model, parsed_formula = spreg.sklearn.from_formula(formula, boston_df, w=weights, debug=True)
    >>> print(type(model))
    <class 'spreg.sklearn.lag.Lag'>
    >>> print(parsed_formula)
    log(CMEDV) ~ {RM**2} + AGE + INDUS + {w.sparse @ `INDUS`} - 1

    Check out the results!

    >>> np.around(model.coef_, decimals=4)
    array([[-0.0014, -0.0021,  0.0233, -0.0013]])
    >>> np.around(model.indir_coef_, decimals=4)
    array([0.0002])
    >>> np.around(model.intercept_, decimals=4)
    array([2.4616])
    """

    # Error checking. Minimum formula size is 5, e.g. "a ~ b"
    if type(formula) != str or len(formula) < 5:
        raise ValueError("Malformed formula string")
    
    lag_start_idx = formula.find("<")
    lag_end_idx = -1
    parsed_formula = formula[:lag_start_idx] if lag_start_idx >= 0 else ""

    y_name = formula.split("~")[0].strip()  # get name of y field, including transformations
    lag_model = False  # flag set to true if spatial lag of y component found
    spatial_model = False  # flag set to true if any spatial component found

    # Parse spatial lags in formula
    while lag_start_idx > 0:
        spatial_model = True
        lag_end_idx = formula.find(">", lag_start_idx + 1)
        if lag_end_idx < 0:
            raise ValueError("Mismatched angle brackets")
        lag_str = formula[lag_start_idx + 1:lag_end_idx]  # get everything btwn brackets

        # Parse lag_str
        fields = lag_str.split(" + ")
        if y_name in fields:
            lag_model = True
            fields.remove(y_name)

        # Default includes covariates and lags
        if len(fields) > 0:
            parsed_formula += " + ".join(fields) + " + " 
            parsed_formula += expand_lag("w", fields)  # spatial weights matrix is w in this scope
        else:  # chomp hanging plus
            parsed_formula = parsed_formula[:-3]

        # Progress parser
        lag_start_idx = formula.find("<", lag_start_idx + 1)
        if lag_start_idx >= 0:
            parsed_formula += formula[lag_end_idx + 1:lag_start_idx]

    parsed_formula += formula[lag_end_idx + 1:]  # add rest of the formula

    # Parse spatial errors in formula
    lhs, rhs = parsed_formula.split(" ~ ")
    err_model = True if "&" in rhs.split(" + ") else False
    spatial_model |= err_model
    if err_model:  # remove & from formula
        no_err = noninplace_remove(rhs.split(" + "), "&")
        parsed_formula = lhs + " ~ " + (" + ".join(no_err) if type(no_err) == list else no_err)

    if spatial_model and w is None:
        raise ValueError("Requested spatial model but did not provide weights matrix")

    # Remove intercept term from formulaic (spreg adds this already)
    parsed_formula += " - 1"
    
    # Assemble model matrices
    if type(df) == gpd.GeoDataFrame:
        df = pd.DataFrame(df)
    y, X = model_matrix(parsed_formula, df)

    y = np.array(y)
    X = np.array(X)  

    method = method.lower()
    if method not in ["gm", "full", "lu", "ord"]:
        raise ValueError(f"Method must be 'gm', 'full', 'lu', 'ord'; was {method}")

    if not (err_model or lag_model):
        model = LinearRegression(**init_kwargs)
        model = model.fit(X, y, **fit_kwargs)
    elif err_model and lag_model:
        raise ValueError("Combo models not supported in sklearn style")
    elif lag_model:
        model = Lag(w=w, **init_kwargs)
        model = model.fit(X, y, method=method, **fit_kwargs)
    elif err_model:
        model = Error(w=w, **init_kwargs)
        model = model.fit(X, y, method=method, **fit_kwargs)

    if debug:
        return model, parsed_formula
    return model