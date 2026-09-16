# S3 Provider

## 1. Overview

The S3 Provider implements the infrastructure storage functionality used by the application for streaming data uploads to Amazon S3.

The implementation separates application-level streaming operations from AWS-specific storage logic through an infrastructure abstraction layer. This allows the application to interact with a common stream interface while keeping provider-specific functionality within the corresponding implementation.


## 2. Architecture

The infrastructure layer follows a provider-based abstraction.

```text
Application
    |
    v
open_stream()
    |
    v
ProviderFactory
    |
    v
AWSStreamUpload
    |
    v
Amazon S3
```

The application obtains a stream through `open_stream()` rather than directly creating an `AWSStreamUpload` instance.

`ProviderFactory` is responsible for selecting and creating the implementation corresponding to the configured infrastructure provider.

## 3. Components

### 3.1 InfraStream

`InfraStream` defines the common interface for infrastructure streaming operations.

Provider-specific stream implementations implement this interface so that the application does not need to depend directly on a particular cloud provider.

The interface provides the contract for stream operations such as writing data and closing the stream.

---

### 3.2 ProviderType

`ProviderType` defines the supported infrastructure providers.

The AWS provider is represented by:

```text
ProviderType.AWS
```

The provider type is used by the factory to determine which infrastructure implementation should be created.

---

### 3.3 ProviderFactory

`ProviderFactory` is responsible for creating the infrastructure stream implementation based on the configured provider.

For the AWS provider, the factory creates an `AWSStreamUpload` instance.

```text
ProviderType.AWS
       |
       v
ProviderFactory
       |
       v
AWSStreamUpload
```

If an unsupported provider is configured, the factory raises a `RuntimeError`.

---

### 3.4 open_stream()

`open_stream()` is the application-facing entry point for obtaining an infrastructure stream.

It:

1. Retrieves the active provider configuration.
2. Creates a `ProviderFactory` using the configured provider.
3. Delegates stream creation to the factory.
4. Returns the provider-specific `InfraStream` implementation.

This keeps provider-selection logic separate from the application code that performs streaming operations.

---

## 4. AWSStreamUpload

`AWSStreamUpload` is the AWS-specific implementation of `InfraStream`.

It is responsible for streaming data to an Amazon S3 object.

The implementation provides the following functionality:

* Writing data to the stream.
* Uploading the stream to S3.
* Closing the stream.
* Configuring S3 object metadata.
* Surfacing upload failures on close, after the AWS SDK's own retries are exhausted.

---

## 5. Streaming Upload Flow

The streaming upload follows the flow below:

```text
Application
    |
    | open_stream(key, settings)
    v
ProviderFactory
    |
    | create_stream()
    v
AWSStreamUpload
    |
    | write_line()
    v
Internal Stream
    |
    | close()
    v
S3 Upload
    |
    v
Amazon S3
```

### 5.1 Stream Creation

The application calls:

```text
open_stream(key, settings)
```

The active infrastructure provider is obtained from the provider configuration.

For AWS, `ProviderFactory` creates an `AWSStreamUpload` instance using the supplied object key and application settings.

---

### 5.2 Writing Data

Data is written to the stream using:

```text
write_line()
```

The data is passed through the internal streaming pipeline rather than requiring the complete dataset to be held in application memory.

---

### 5.3 Closing the Stream

When:

```text
close()
```

is called, the stream is finalized and the upload process is completed.

The resulting object is stored in the configured S3 bucket using the specified object key.

---

## 6. S3 Upload Configuration

The S3 upload uses the configured AWS region and S3 bucket together with the object key provided when the stream is created.

The uploaded object is configured with the following metadata:

| Property         | Value                  |
| ---------------- | ---------------------- |
| Content Type     | `application/x-ndjson` |
| Content Encoding | `gzip`                 |

The content type identifies the data as newline-delimited JSON, while the content encoding indicates that the uploaded content is gzip-compressed.

---

## 7. Retry and Failure Handling

Retries are performed by the AWS SDK, not by this code. The upload streams from an OS pipe, which cannot be rewound — retrying the whole upload would re-send only the unconsumed tail and store a truncated object. botocore instead retries an individual multipart part, which it can replay from its own buffer.

The client is configured with:

1. `storage_retry_attempts` — retries per request, in `adaptive` mode (adds client-side rate limiting). botocore counts *retries*, so 3 allows up to 4 total attempts.
2. `storage_connect_timeout` — seconds to establish a connection.
3. `storage_read_timeout` — seconds to wait for a response.

If the SDK's retries are exhausted, the error is recorded and re-raised by `close()`. `close()` never returns a storage key for an incomplete object, so neither the caller nor the backend notification is pointed at a corrupt upload.

---

## 8. Configuration

The S3 implementation uses application settings for AWS configuration.

Relevant configuration includes:

| Configuration            | Purpose                                  |
| ------------------------ | ---------------------------------------- |
| `aws_s3_bucket`          | S3 bucket used for uploads               |
| `aws_region`             | AWS region used by the S3 client         |
| `aws_credentials_kwargs` | AWS credential configuration             |
| `storage_retry_attempts` | Retries per request, applied by the SDK  |
| `storage_connect_timeout`| Connection timeout in seconds            |
| `storage_read_timeout`   | Response timeout in seconds              |

Configuration values should be provided through the application's configuration mechanism rather than being hardcoded in the implementation.

---

## 9. Unit Testing

Unit tests have been added to validate both the AWS S3 implementation and the infrastructure provider abstraction.

### 9.1 AWSStreamUpload Tests

| Test Case                                 | Purpose                                                                           |
| ----------------------------------------- | --------------------------------------------------------------------------------- |
| `test_write_line()`                       | Verifies that data can be written to the internal stream.                         |
| `test_close()`                            | Verifies that the stream closes successfully and returns the expected object key. |
| `test_upload_calls_s3()`                  | Verifies that the upload operation is triggered when the stream is closed.        |
| `test_upload_uses_correct_s3_arguments()` | Verifies the S3 bucket, object key, content type, and content encoding.           |

This means the actual `AWSStreamUpload` implementation is executed during the test, while the S3 client's network operation is mocked. Therefore, the tests validate the application's upload logic without making an actual request to AWS.

---

### 9.2 ProviderFactory Test

The factory test verifies that the AWS provider creates the expected AWS stream implementation.

The test validates the following flow:

```text
ProviderType.AWS
       |
       v
ProviderFactory
       |
       v
AWSStreamUpload
```

It also verifies that the expected object key and settings are passed to `AWSStreamUpload`.

---

### 9.3 Provider Test

The provider test verifies that `open_stream()` correctly uses the active provider configuration and delegates stream creation through `ProviderFactory`.

The expected flow is:

```text
open_stream()
     |
     v
Provider Configuration
     |
     v
ProviderFactory
     |
     v
AWSStreamUpload
```

---

## 10. Test Execution

The S3 unit tests can be executed using:

```bash
pytest -q src/tests_infra/aws/test_S3.py
```

The factory tests can be executed using:

```bash
pytest -q src/tests_infra/test_factory.py
```

The provider tests can be executed using:

```bash
pytest -q src/tests_infra/test_provider.py
```

The tests should be executed after changes to the S3 implementation, provider abstraction, or related configuration.

---

## 11. Deployment Testing

The unit tests use a mocked S3 client and therefore do not validate connectivity to a real S3 bucket.

After deployment, real S3 scenarios should be tested in the configured environment.

Recommended deployment scenarios include:

| # | Scenario                                  | Expected Result                                    |
| - | ----------------------------------------- | -------------------------------------------------- |
| 1 | Create an S3 stream and upload data       | Upload completes successfully                      |
| 2 | Write multiple lines and close the stream | All data is uploaded successfully                  |
| 3 | Verify the uploaded object                | Object exists in the configured S3 bucket          |
| 4 | Verify object metadata                    | Content type and encoding are configured correctly |
| 5 | Temporary connection failure              | Upload retries according to configuration          |
| 6 | Persistent upload failure                 | Upload fails after the configured retry            |

The exact deployment scenarios should follow the application's environment and project-specific testing requirements.

---

## 12. Deployment Validation

Deployment-level testing should confirm that the complete application flow works with a real S3 environment.

The validation flow is:

```text
Deployed Application
        |
        v
open_stream()
        |
        v
ProviderFactory
        |
        v
AWSStreamUpload
        |
        v
AWS S3
        |
        v
Verify Uploaded Object
```

The following should be verified during deployment testing:

* The application can create an S3 stream.
* Data can be written successfully.
* The stream can be closed successfully.
* The object is created in the expected S3 bucket.
* The object key is correct.
* The uploaded object has the expected content type and encoding.
* Retry behavior works as expected when applicable.

---

## 13. Provider Abstraction

The infrastructure layer is designed so that application code does not need to directly depend on AWS-specific classes.

The application interacts with:

```text
InfraStream
```

while the provider-specific implementation is selected through:

```text
ProviderFactory
```

For AWS:

```text
InfraStream
     ^
     |
AWSStreamUpload
```

This provides a clear separation between the application-level interface and provider-specific implementation.

---

## 14. Adding a New Infrastructure Provider

To add another infrastructure provider:

1. Add the provider to `ProviderType`.
2. Implement the `InfraStream` interface.
3. Create the provider-specific stream implementation.
4. Add the provider implementation to `ProviderFactory`.
5. Update provider configuration as required.
6. Add unit tests for the new provider.
7. Perform deployment-level testing.

The application-level streaming flow remains unchanged:

```text
Application
    |
    v
open_stream()
    |
    v
ProviderFactory
    |
    v
Provider-specific implementation
```

This allows provider-specific implementation details to remain isolated from the application.

---

## 15. Error Handling

The S3 implementation uses Botocore's built-in retry mechanism to handle supported connection-related failures during the upload process.

Upload Failure
      |
      v
Botocore Retry Mechanism
      |
      v
Retry Request
      |
      v
Retry Attempts Exhausted?
      |
   +--+--+
   |     |
  No    Yes
   |     |
   v     v
Retry   Propagate Error

If the configured retry attempts are exhausted, the upload operation fails and the error is propagated to the caller.

---

## 16. Summary

The S3 Provider provides a streaming storage implementation while keeping AWS-specific functionality isolated behind the infrastructure abstraction.

The overall design is:

```text
Application
     |
     v
open_stream()
     |
     v
ProviderFactory
     |
     v
InfraStream
     |
     v
AWSStreamUpload
     |
     v
Amazon S3
```

The implementation provides:

* A common infrastructure stream interface.
* Centralized provider selection.
* AWS-specific S3 streaming functionality.
* Built-in retry handling through Botocore.
* Unit-testable components.
* Separation between application logic and cloud-provider-specific implementation.
* A clear extension point for additional infrastructure providers.

Unit tests validate the implementation logic, while deployment testing validates the complete flow against the configured S3 environment.